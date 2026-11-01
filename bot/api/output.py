"""把資料庫的資料轉成 API 的回應（需要 S3 產生網址的部分）。"""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from sqlalchemy import select

from bot.api.schemas import (
    AttachmentOut,
    ForwardedOut,
    MentionedChannelOut,
    MentionedRoleOut,
    MessageOut,
    MessagePage,
    RevisionOut,
    UserOut,
)
from bot.db.models import Ticket, TicketAttachment, TicketMessage, TicketUser
from bot.storage.media import sticker_url
from bot.storage.share import avatar_url, download_url
from bot.tickets.history import visible_attachments, visible_revisions
from bot.tickets.mentions import (
    CHANNEL_MENTION,
    ROLE_MENTION,
    USER_MENTION,
    can_view,
    mentioned_ids,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from types_aiobotocore_s3 import S3Client

    from bot.api.deps import Viewer


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


def guild_mentions(
    guild: discord.Guild, viewer: Viewer, texts: list[str], *, staff: bool
) -> tuple[list[MentionedRoleOut], list[MentionedChannelOut]]:
    """內容裡提及的身份組（<@&id>）與頻道（<#id>），名稱用伺服器現在的資料。

    跟 Discord 一樣，看不到的頻道不回傳名稱；客服看得到全部。
    """
    roles = [
        MentionedRoleOut(id=role.id, name=role.name, color=role.color.value)
        for role_id in mentioned_ids(ROLE_MENTION, texts)
        if (role := guild.get_role(role_id)) is not None
    ]
    channel_viewer = None if staff else (viewer.member or guild.default_role)
    channels = [
        MentionedChannelOut(id=channel.id, name=channel.name)
        for channel_id in mentioned_ids(CHANNEL_MENTION, texts)
        if (channel := guild.get_channel_or_thread(channel_id)) is not None
        and can_view(channel, channel_viewer)
    ]
    return roles, channels


async def message_page(
    db: AsyncSession,
    s3: S3Client,
    guild: discord.Guild,
    viewer: Viewer,
    ticket: Ticket,
    messages: list[TicketMessage],
) -> MessagePage:
    """訊息加上附件、編輯前的內容與作者的快照（看得到的範圍見 bot/tickets/history.py）。"""
    staff = viewer.access.is_staff(ticket.category_id)
    attachments = {
        message_id: [await attachment_out(s3, attachment) for attachment in rows]
        for message_id, rows in (await visible_attachments(db, messages, staff=staff)).items()
    }
    revisions = {
        message_id: [RevisionOut.model_validate(revision) for revision in rows]
        for message_id, rows in (await visible_revisions(db, messages, staff=staff)).items()
    }
    # 作者與內容裡被 tag 的人（<@id>）；看不到內容的訊息不算
    texts = [
        text
        for message in messages
        if message.deleted_at is None or staff
        for text in (message.content, message.forwarded_content or "")
    ] + [revision.content for rows in revisions.values() for revision in rows]
    user_ids = {message.author_id for message in messages} | mentioned_ids(USER_MENTION, texts)
    users = await db.scalars(
        select(TicketUser).where(
            TicketUser.ticket_id == ticket.id, TicketUser.user_id.in_(user_ids)
        )
    )

    roles, channels = guild_mentions(guild, viewer, texts, staff=staff)
    page = MessagePage(
        messages=[MessageOut.model_validate(message) for message in messages],
        users=[await user_out(s3, row) for row in users],
        roles=roles,
        channels=channels,
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
