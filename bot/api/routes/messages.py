import logging
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select
from taskiq.exceptions import SendTaskError

from bot.api.deps import DBDep, S3Dep, ViewerDep
from bot.api.output import message_page
from bot.api.routes.tickets import ticket_errors, visible_ticket
from bot.api.schemas import AttachmentOut, MessageIn, MessageOut, MessagePage
from bot.db.models import TicketMessage
from bot.tickets.messages import create_web_message
from bot.worker.tasks import deliver_messages

log = logging.getLogger(__name__)

router = APIRouter(prefix="/tickets")


@router.get("/{ticket_id}/messages")
async def list_messages(
    ticket_id: int,
    viewer: ViewerDep,
    db: DBDep,
    s3: S3Dep,
    before: int | None = None,
    after: int | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> MessagePage:
    """訊息依照順序排列（舊的在前面）。

    沒有指定 before、after 時是最新的 limit 則；before 往前翻頁（用這一頁第一則的 id）；
    after 取得之後的訊息，例如收到 SSE 的 message.created 時。
    """
    ticket = await visible_ticket(db, viewer, ticket_id)
    query = select(TicketMessage).where(TicketMessage.ticket_id == ticket.id)
    if before is not None:
        query = query.where(TicketMessage.id < before)
    if after is not None:
        query = query.where(TicketMessage.id > after)
        messages = list(await db.scalars(query.order_by(TicketMessage.id).limit(limit)))
    else:
        messages = list(await db.scalars(query.order_by(TicketMessage.id.desc()).limit(limit)))
        messages.reverse()
    return await message_page(
        db, s3, ticket, messages, staff=viewer.access.is_staff(ticket.category_id)
    )


@router.get("/{ticket_id}/messages/{message_id}")
async def get_message(
    ticket_id: int, message_id: int, viewer: ViewerDep, db: DBDep, s3: S3Dep
) -> MessagePage:
    """單則訊息，例如收到 SSE 的 message.updated、message.deleted 時重新取得。

    回應的格式跟列表一樣（messages 只有這一則），網頁可以用同一段程式合併。
    """
    ticket = await visible_ticket(db, viewer, ticket_id)
    message = await db.get(TicketMessage, message_id)
    if message is None or message.ticket_id != ticket.id:
        raise HTTPException(404, "找不到這則訊息")
    return await message_page(
        db, s3, ticket, [message], staff=viewer.access.is_staff(ticket.category_id)
    )


@router.post("/{ticket_id}/messages", status_code=201)
async def create_message(
    ticket_id: int, body: MessageIn, viewer: ViewerDep, db: DBDep
) -> MessageOut:
    """從網頁送出訊息。worker 透過 webhook 送到 Discord 後，用 SSE 的 message.updated 通知。"""
    ticket = await visible_ticket(db, viewer, ticket_id)
    with ticket_errors():
        message, attachments = await create_web_message(
            db, ticket, viewer.member or viewer.user, body.content, body.attachment_ids
        )
    try:
        await deliver_messages.kiq(ticket.id)
    except SendTaskError:
        # requeue_pending_messages 之後會補放
        log.exception("Failed to queue deliver_messages for ticket %d", ticket.id)
    out = MessageOut.model_validate(message)
    out.attachments = [AttachmentOut.model_validate(a) for a in attachments]
    return out
