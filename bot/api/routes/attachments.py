import logging

from fastapi import APIRouter, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from taskiq.exceptions import SendTaskError

from bot.api.deps import DBDep, S3Dep, Viewer, ViewerDep
from bot.api.routes.tickets import ticket_errors, visible_ticket
from bot.api.schemas import AttachmentIn, AttachmentOut, PartsIn, PartUrlOut, UploadOut
from bot.db.models import Source, TicketAttachment
from bot.tickets import attachments
from bot.worker.tasks import verify_attachment

log = logging.getLogger(__name__)

router = APIRouter()


async def own_attachment(db: AsyncSession, viewer: Viewer, attachment_id: int) -> TicketAttachment:
    """自己在網頁上傳的附件；其他人的一律回傳 404。"""
    attachment = await db.get(TicketAttachment, attachment_id)
    if (
        attachment is None
        or attachment.source is not Source.WEB
        or attachment.uploader_id != viewer.id
    ):
        raise HTTPException(404, "找不到這個附件")
    # 被移出客服單之後就不能再操作
    await visible_ticket(db, viewer, attachment.ticket_id)
    return attachment


@router.post("/tickets/{ticket_id}/attachments", status_code=201)
async def create_attachment(
    ticket_id: int, body: AttachmentIn, viewer: ViewerDep, db: DBDep, s3: S3Dep
) -> UploadOut:
    """申請上傳網址。檔案由瀏覽器直接上傳到 S3，不經過 bot。"""
    ticket = await visible_ticket(db, viewer, ticket_id)
    with ticket_errors():
        upload = await attachments.create_upload(
            db, s3, ticket, viewer.member or viewer.user, filename=body.filename, size=body.size
        )
    return UploadOut(
        attachment=AttachmentOut.model_validate(upload.attachment),
        url=upload.url,
        part_size=upload.part_size,
        part_count=upload.part_count,
    )


@router.post("/attachments/{attachment_id}/parts")
async def sign_parts(
    attachment_id: int, body: PartsIn, viewer: ViewerDep, db: DBDep, s3: S3Dep
) -> list[PartUrlOut]:
    """分塊上傳時，每一塊的上傳網址。"""
    attachment = await own_attachment(db, viewer, attachment_id)
    with ticket_errors():
        urls = await attachments.sign_parts(s3, attachment, body.part_numbers)
    return [PartUrlOut(part_number=number, url=url) for number, url in urls]


@router.post("/attachments/{attachment_id}/complete")
async def complete(attachment_id: int, viewer: ViewerDep, db: DBDep, s3: S3Dep) -> AttachmentOut:
    """上傳完成。之後由 worker 檢查檔案內容，結果透過 SSE 的 attachment.updated 通知。"""
    attachment = await own_attachment(db, viewer, attachment_id)
    with ticket_errors():
        completed = await attachments.complete_upload(db, s3, attachment)
    if completed:
        try:
            await verify_attachment.kiq(attachment.id)
        except SendTaskError:
            # requeue_pending_attachments 之後會補放
            log.exception("Failed to queue verify_attachment %d", attachment.id)
    return AttachmentOut.model_validate(attachment)


@router.delete("/attachments/{attachment_id}", status_code=204)
async def remove(attachment_id: int, viewer: ViewerDep, db: DBDep, s3: S3Dep) -> None:
    """移除還沒送出的附件。"""
    attachment = await own_attachment(db, viewer, attachment_id)
    with ticket_errors():
        await attachments.discard_attachment(db, s3, attachment)
