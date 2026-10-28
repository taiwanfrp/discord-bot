"""把資料庫的資料轉成 API 的回應（需要 S3 產生網址的部分）。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from bot.api.schemas import AttachmentOut, MessageOut, MessagePage, RevisionOut, UserOut
from bot.db.models import Ticket, TicketAttachment, TicketMessage, TicketMessageRevision, TicketUser
from bot.storage.media import sticker_url
from bot.storage.share import avatar_url, download_url

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from types_aiobotocore_s3 import S3Client


async def user_out(s3: S3Client, row: TicketUser) -> UserOut:
    out = UserOut.model_validate(row)
    out.avatar_url = await avatar_url(s3, row)
    return out


async def attachment_out(s3: S3Client, attachment: TicketAttachment) -> AttachmentOut:
    out = AttachmentOut.model_validate(attachment)
    out.download_url = await download_url(s3, attachment)
    return out


async def message_page(
    db: AsyncSession, s3: S3Client, ticket: Ticket, messages: list[TicketMessage], *, staff: bool
) -> MessagePage:
    """訊息加上附件、編輯前的內容與作者的快照。

    被刪除的訊息、編輯前的內容、編輯時移除的附件，只有這張單的客服、管理員（staff）看得到
    （docs/tickets.md 8.1）。
    """
    message_ids = [message.id for message in messages if staff or message.deleted_at is None]
    attachment_query = select(TicketAttachment).where(TicketAttachment.message_id.in_(message_ids))
    if not staff:
        attachment_query = attachment_query.where(TicketAttachment.removed_at.is_(None))
    attachments: dict[int, list[AttachmentOut]] = {}
    for attachment in await db.scalars(attachment_query.order_by(TicketAttachment.id)):
        assert attachment.message_id is not None
        attachments.setdefault(attachment.message_id, []).append(
            await attachment_out(s3, attachment)
        )
    revisions: dict[int, list[RevisionOut]] = {}
    if staff:
        for revision in await db.scalars(
            select(TicketMessageRevision)
            .where(
                TicketMessageRevision.message_id.in_(
                    [message.id for message in messages if message.edited_at is not None]
                )
            )
            .order_by(TicketMessageRevision.id)
        ):
            revisions.setdefault(revision.message_id, []).append(
                RevisionOut.model_validate(revision)
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
        if out.deleted_at is not None and not staff:
            out.content = ""
            out.stickers = []
        for sticker in out.stickers:
            sticker.url = sticker_url(sticker.id, sticker.format)
        out.attachments = attachments.get(out.id, [])
        out.revisions = revisions.get(out.id, [])
    return page
