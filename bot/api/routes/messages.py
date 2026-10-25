import logging
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import select
from taskiq.exceptions import SendTaskError

from bot.api.deps import DBDep, S3Dep, ViewerDep
from bot.api.output import attachment_out, user_out
from bot.api.routes.tickets import ticket_errors, visible_ticket
from bot.api.schemas import AttachmentOut, MessageIn, MessageOut, MessagePage
from bot.db.models import TicketAttachment, TicketMessage, TicketUser
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
    after 取得之後的訊息，例如收到 SSE 的 message.created、message.updated 時。
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

    message_ids = [message.id for message in messages]
    attachments: dict[int, list[AttachmentOut]] = {}
    for attachment in await db.scalars(
        select(TicketAttachment)
        .where(TicketAttachment.message_id.in_(message_ids))
        .order_by(TicketAttachment.id)
    ):
        assert attachment.message_id is not None
        attachments.setdefault(attachment.message_id, []).append(
            await attachment_out(s3, attachment)
        )
    authors = await db.scalars(
        select(TicketUser).where(
            TicketUser.ticket_id == ticket.id,
            TicketUser.user_id.in_({message.author_id for message in messages}),
        )
    )

    page = MessagePage(
        messages=[MessageOut.model_validate(message) for message in messages],
        users=[await user_out(s3, row) for row in authors],
    )
    for out in page.messages:
        out.attachments = attachments.get(out.id, [])
    return page


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
