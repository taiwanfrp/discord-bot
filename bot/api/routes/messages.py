import logging

from fastapi import APIRouter
from taskiq.exceptions import SendTaskError

from bot.api.deps import DBDep, ViewerDep
from bot.api.routes.tickets import ticket_errors, visible_ticket
from bot.api.schemas import AttachmentOut, MessageIn, MessageOut
from bot.tickets.messages import create_web_message
from bot.worker.tasks import deliver_messages

log = logging.getLogger(__name__)

router = APIRouter(prefix="/tickets")


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
