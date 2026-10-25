import discord
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import (
    AttachmentStatus,
    DeliveryStatus,
    Source,
    Ticket,
    TicketAttachment,
    TicketEvent,
    TicketMessage,
)
from bot.db.queries import execute_update, get_guild_settings
from bot.storage.files import allowed_file_types, is_allowed_extension
from bot.tickets.actions import require_open
from bot.tickets.delivery import MAX_LENGTH, max_length
from bot.tickets.errors import TicketError
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


async def create_web_message(
    db: AsyncSession,
    ticket: Ticket,
    author: discord.User | discord.Member,
    content: str,
    attachment_ids: list[int],
) -> tuple[TicketMessage, list[TicketAttachment]]:
    """網頁送出的訊息加密後存進資料庫，之後由 deliver_messages 送到 Discord（docs/tickets.md 8.2）。

    附件要是這個人在這張單上傳完、還沒送出的。檢查還沒完成的也可以，訊息會等它檢查完再送。
    """
    require_open(ticket)
    if not content.strip() and not attachment_ids:
        raise TicketError("訊息不能是空的", 422)
    if len(set(attachment_ids)) != len(attachment_ids):
        raise TicketError("附件重複了", 422)
    attachments = list(
        await db.scalars(
            select(TicketAttachment)
            .where(TicketAttachment.id.in_(attachment_ids))
            .order_by(TicketAttachment.id)
        )
    )
    usable = {
        a.id
        for a in attachments
        if a.ticket_id == ticket.id
        and a.uploader_id == author.id
        and a.source is Source.WEB
        and a.uploaded_at is not None
        and a.message_id is None
        and a.status in (AttachmentStatus.PENDING, AttachmentStatus.DONE)
    }
    if usable != set(attachment_ids):
        raise TicketError("有附件還沒上傳完成、沒有通過檢查，或是已經送出過了", 422)
    if max_length(content, attachments) > MAX_LENGTH:
        raise TicketError(
            f"訊息加上附件連結超過 Discord 的 {MAX_LENGTH} 字上限，請縮短內容或分開送出", 422
        )

    message = TicketMessage(
        ticket_id=ticket.id,
        source=Source.WEB,
        author_id=author.id,
        content=content,
        delivery_status=DeliveryStatus.PENDING,
    )
    db.add(message)
    await db.flush()
    if attachment_ids:
        # 帶條件的 UPDATE：同一個附件同時被放進兩則訊息時，只有一則會成功
        attached = await execute_update(
            db,
            update(TicketAttachment)
            .where(TicketAttachment.id.in_(attachment_ids), TicketAttachment.message_id.is_(None))
            .values(message_id=message.id),
        )
        if attached != len(attachment_ids):
            await db.rollback()
            raise TicketError("有附件已經送出過了", 409)

    await update_snapshot(db, ticket.id, author)
    ticket.last_message_at = message.created_at
    # 有新訊息就重新計算閒置時間（docs/tickets.md 第 9 節）
    ticket.inactivity_reminded_at = None
    db.add(
        TicketEvent(
            ticket_id=ticket.id,
            type="message.created",
            actor_id=author.id,
            data={"message_id": str(message.id)},
        )
    )
    await db.commit()
    for attachment in attachments:
        await db.refresh(attachment)
    return message, attachments
