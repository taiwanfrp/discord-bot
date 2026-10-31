"""把資料庫的資料轉成 API 的回應（需要 S3 產生網址的部分）。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from bot.api.schemas import (
    AttachmentOut,
    ForwardedOut,
    MessageOut,
    MessagePage,
    RevisionOut,
    UserOut,
)
from bot.db.models import Ticket, TicketAttachment, TicketMessage, TicketUser
from bot.storage.media import sticker_url
from bot.storage.share import avatar_url, download_url
from bot.tickets.history import visible_attachments, visible_revisions

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


def forwarded_out(message: TicketMessage) -> ForwardedOut | None:
    if message.forwarded_from is None:
        return None
    out = ForwardedOut.model_validate(
        {**message.forwarded_from, "content": message.forwarded_content or ""}
    )
    for sticker in out.stickers:
        sticker.url = sticker_url(sticker.id, sticker.format)
    return out


async def message_page(
    db: AsyncSession, s3: S3Client, ticket: Ticket, messages: list[TicketMessage], *, staff: bool
) -> MessagePage:
    """訊息加上附件、編輯前的內容與作者的快照（看得到的範圍見 bot/tickets/history.py）。"""
    attachments = {
        message_id: [await attachment_out(s3, attachment) for attachment in rows]
        for message_id, rows in (await visible_attachments(db, messages, staff=staff)).items()
    }
    revisions = {
        message_id: [RevisionOut.model_validate(revision) for revision in rows]
        for message_id, rows in (await visible_revisions(db, messages, staff=staff)).items()
    }
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
    by_id = {message.id: message for message in messages}
    for out in page.messages:
        if out.deleted_at is not None and not staff:
            out.content = ""
            out.stickers = []
        else:
            out.forwarded = forwarded_out(by_id[out.id])
        for sticker in out.stickers:
            sticker.url = sticker_url(sticker.id, sticker.format)
        out.attachments = attachments.get(out.id, [])
        out.revisions = revisions.get(out.id, [])
    return page
