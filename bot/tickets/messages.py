import discord
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import (
    AttachmentStatus,
    Source,
    Ticket,
    TicketAttachment,
    TicketEvent,
    TicketMessage,
)
from bot.db.queries import get_guild_settings
from bot.storage.files import allowed_file_types, is_allowed_extension
from bot.tickets.snapshot import update_snapshot


async def save_discord_message(
    db: AsyncSession, ticket: Ticket, message: discord.Message
) -> list[int]:
    """把客服單頻道裡的 Discord 訊息加密後存進資料庫（docs/tickets.md 8.1）。

    回傳要放入 mirror_attachment 的附件 id。同一則訊息重複處理時（例如重新連線後
    收到重複的事件）什麼都不做。
    """
    saved = TicketMessage(
        ticket_id=ticket.id,
        source=Source.DISCORD,
        discord_message_id=message.id,
        author_id=message.author.id,
        content=message.content,
        created_at=message.created_at,
    )
    db.add(saved)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        return []

    await update_snapshot(db, ticket.id, message.author)
    ticket.last_message_at = message.created_at
    # 有新訊息就重新計算閒置時間（docs/tickets.md 第 9 節）
    ticket.inactivity_reminded_at = None

    settings = await get_guild_settings(db)
    allowed = allowed_file_types(settings)
    attachments: list[TicketAttachment] = []
    for attachment in message.attachments:
        # 副檔名與大小先在這裡檢查；檔案內容（magic bytes）由 worker 下載時檢查
        ok = attachment.size <= settings.max_attachment_size and is_allowed_extension(
            attachment.filename, allowed
        )
        attachments.append(
            TicketAttachment(
                ticket_id=ticket.id,
                message_id=saved.id,
                uploader_id=message.author.id,
                source=Source.DISCORD,
                discord_attachment_id=attachment.id,
                source_url=attachment.url,
                filename=attachment.filename[:255],
                content_type=attachment.content_type,
                size=attachment.size,
                status=AttachmentStatus.PENDING if ok else AttachmentStatus.REJECTED,
            )
        )
    db.add_all(attachments)
    db.add(
        TicketEvent(
            ticket_id=ticket.id,
            type="message.created",
            actor_id=message.author.id,
            data={"message_id": str(saved.id)},
        )
    )
    await db.commit()
    return [a.id for a in attachments if a.status is AttachmentStatus.PENDING]
