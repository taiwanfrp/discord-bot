"""匯出客服單（docs/tickets.md 8.8）：由 worker 產生，網頁定時查詢狀態，完成後下載。"""

import logging
from datetime import timedelta

from fastapi import APIRouter, HTTPException
from sqlalchemy import func, select
from taskiq.exceptions import SendTaskError

from bot.api.deps import DBDep, S3Dep, ViewerDep
from bot.api.routes.tickets import visible_ticket
from bot.api.schemas import ExportIn, ExportOut
from bot.db.models import ExportStatus, TicketExport
from bot.db.types import utcnow
from bot.tickets import export
from bot.worker.tasks import export_ticket

log = logging.getLogger(__name__)

router = APIRouter()

# 每個人每小時最多匯出幾次：有大附件的單，每次都會在 S3 多一份完整的 ZIP（保留 24 小時）
RATE_LIMIT = 10
RATE_WINDOW = timedelta(hours=1)


@router.post("/tickets/{ticket_id}/exports", status_code=201)
async def create_export(ticket_id: int, body: ExportIn, viewer: ViewerDep, db: DBDep) -> ExportOut:
    """開單者、這張單的客服、管理員可以匯出，內容跟各自在網頁上看到的一樣。

    同一個人在同一張單還有沒完成的匯出時，回傳那一個（例如連按兩次）。
    """
    ticket = await visible_ticket(db, viewer, ticket_id)
    staff = viewer.access.is_staff(ticket.category_id)
    if not staff and viewer.id != ticket.opener_id:
        raise HTTPException(403, "只有開單者與客服可以匯出")
    existing = await db.scalar(
        select(TicketExport).where(
            TicketExport.ticket_id == ticket.id,
            TicketExport.requested_by_id == viewer.id,
            TicketExport.staff == staff,
            TicketExport.status == ExportStatus.PENDING,
        )
    )
    if existing is not None:
        return ExportOut.model_validate(existing)
    recent = await db.scalar(
        select(func.count())
        .select_from(TicketExport)
        .where(
            TicketExport.requested_by_id == viewer.id,
            TicketExport.created_at > utcnow() - RATE_WINDOW,
        )
    )
    if recent is not None and recent >= RATE_LIMIT:
        raise HTTPException(429, "匯出太頻繁，請稍後再試")

    row = TicketExport(
        ticket_id=ticket.id, requested_by_id=viewer.id, staff=staff, utc_offset=body.utc_offset
    )
    db.add(row)
    await db.commit()
    try:
        await export_ticket.kiq(row.id)
    except SendTaskError:
        log.exception("Failed to queue export_ticket for export %d", row.id)
        row.status = ExportStatus.FAILED
        row.finished_at = utcnow()
        await db.commit()
    return ExportOut.model_validate(row)


@router.get("/exports/{export_id}")
async def get_export(export_id: int, viewer: ViewerDep, db: DBDep, s3: S3Dep) -> ExportOut:
    """只有匯出的人自己拿得到；完成時附上下載網址（每次都是新的）。"""
    row = await db.get(TicketExport, export_id)
    if row is None or row.requested_by_id != viewer.id:
        raise HTTPException(404, "找不到這個匯出")
    out = ExportOut.model_validate(row)
    out.download_url = await export.download_url(s3, row)
    return out
