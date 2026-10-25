"""把網頁的訊息透過 webhook 送到 Discord（docs/tickets.md 8.2），由 worker 執行。"""

import contextlib
import logging
import re
from collections.abc import Sequence

import discord
from redis.asyncio import Redis
from redis.exceptions import LockError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import (
    AttachmentStatus,
    DeliveryStatus,
    Source,
    Ticket,
    TicketAttachment,
    TicketEvent,
    TicketMessage,
    TicketStatus,
    TicketUser,
)
from bot.storage.share import TOKEN_LENGTH, new_share_token, share_url
from bot.tickets.snapshot import display_name

log = logging.getLogger(__name__)

# Discord 訊息內容的長度上限
MAX_LENGTH = 2000
# 附件連結顯示的檔名最多幾個字
MAX_LINK_TEXT = 60
# 鎖的期限；每送出一則訊息就重新計算，所以只要比送出一則訊息的時間長
LOCK_TIMEOUT = 10 * 60
# 另一個 worker 正在送這張單的訊息時，最多等多久
LOCK_WAIT = 30
# Discord 不接受使用者的名稱時改用這個
FALLBACK_NAME = "網頁使用者"


class DeliveryBusy(Exception):
    """另一個 worker 正在送這張單的訊息，或是送到一半當機了（鎖還沒過期）。"""


class ChannelGone(Exception):
    """客服單頻道已經被刪除。"""


def link_text(filename: str) -> str:
    name = filename if len(filename) <= MAX_LINK_TEXT else filename[: MAX_LINK_TEXT - 1] + "…"
    # 方括號會讓連結的文字提早結束
    return discord.utils.escape_markdown(name.replace("[", "(").replace("]", ")"))


def attachment_line(attachment: TicketAttachment, url: str | None) -> str:
    """url 是 None 表示這個附件沒有通過檢查，只顯示檔名。"""
    name = link_text(attachment.filename)
    if url is not None:
        return f"📎 [{name}]({url})"
    reason = "檔案類型不符" if attachment.status is AttachmentStatus.REJECTED else "處理失敗"
    return f"📎 {name}（{reason}，沒有傳送）"


def render(content: str, lines: Sequence[str]) -> str:
    return "\n".join(part for part in (content, *lines) if part)


def max_length(content: str, attachments: Sequence[TicketAttachment]) -> int:
    """送出時最長會有多長：每個附件都通過檢查（連結比沒有通過時的說明長）。"""
    placeholder = "x" * TOKEN_LENGTH
    lines = [attachment_line(a, share_url(a.id, placeholder)) for a in attachments]
    return len(render(content, lines))


def webhook_name(row: TicketUser | None) -> str:
    name = display_name(row) if row is not None else ""
    # Discord 不接受含有 discord、clyde 的 webhook 名稱，在中間插入間隔號。實測零寬空白、
    # 外觀相似的字母（例如西里爾字母 о）、全形字母都會被 Discord 當成同一個字
    name = re.sub(r"(?i)discord|clyde", lambda m: f"{m[0][:2]}\u00b7{m[0][2:]}", name).strip()
    return name[:80] or FALLBACK_NAME


def avatar_url(row: TicketUser | None, user_id: int) -> str:
    if row is not None and row.discord_avatar_url:
        return row.discord_avatar_url
    # 沒有自訂頭像：Discord 的預設頭像
    return f"https://cdn.discordapp.com/embed/avatars/{(user_id >> 22) % 6}.png"


def webhook_for(discord_client: discord.Client, ticket: Ticket) -> discord.Webhook:
    assert ticket.webhook_id is not None and ticket.webhook_token is not None
    return discord.Webhook.partial(ticket.webhook_id, ticket.webhook_token, client=discord_client)


async def deliver_messages(
    db: AsyncSession,
    redis: Redis,
    discord_client: discord.Client,
    ticket_id: int,
    *,
    last_attempt: bool,
) -> None:
    """依照順序送出這張單所有待送的網頁訊息（8.2）。

    同一張單同時只有一個 worker 在送。附件還在檢查的訊息會停下來，後面的訊息也不會插隊；
    附件檢查完之後，verify_attachment 會再放入這個工作。送出失敗時丟出例外讓 queue 重試，
    last_attempt（最後一次重試）時改成標記為 failed，繼續送下一則。
    """
    lock = redis.lock(
        f"ticket-bot:lock:deliver:{ticket_id}", timeout=LOCK_TIMEOUT, blocking_timeout=LOCK_WAIT
    )
    if not await lock.acquire():
        raise DeliveryBusy(ticket_id)
    try:
        while (message := await next_pending(db, ticket_id)) is not None:
            ticket = await db.get_one(Ticket, ticket_id)
            if ticket.status is not TicketStatus.OPEN or ticket.channel_id is None:
                # 頻道已經刪除，送不出去了
                await set_status(db, message, DeliveryStatus.FAILED)
                continue
            attachments = list(
                await db.scalars(
                    select(TicketAttachment)
                    .where(TicketAttachment.message_id == message.id)
                    .order_by(TicketAttachment.id)
                )
            )
            if any(a.status is AttachmentStatus.PENDING for a in attachments):
                return

            message.attempts += 1
            lines: list[str] = []
            for attachment in attachments:
                url = None
                if attachment.status is AttachmentStatus.DONE:
                    # 每次都產生新的 token（資料庫只有雜湊，重試時拿不到上一次的 token）
                    token, attachment.share_token_hash = new_share_token()
                    url = share_url(attachment.id, token)
                lines.append(attachment_line(attachment, url))
            # 先存好 token 再送出：Discord 收到訊息後馬上就會來抓預覽
            await db.commit()

            content = render(message.content, lines)
            try:
                sent = await send(db, discord_client, ticket, message, content)
            except ChannelGone:
                await set_status(db, message, DeliveryStatus.FAILED)
                continue
            except Exception as error:
                # 400 表示內容有問題，重試也一樣會失敗
                invalid = isinstance(error, discord.HTTPException) and error.status == 400
                if not invalid and not last_attempt:
                    raise
                log.exception("Failed to deliver message %d", message.id)
                await set_status(db, message, DeliveryStatus.FAILED)
                continue
            message.discord_message_id = sent.id
            await set_status(db, message, DeliveryStatus.SENT)
            # 送出一則就延長鎖的期限；鎖已經過期的話丟出例外，交給重試
            await lock.reacquire()
    finally:
        with contextlib.suppress(LockError):
            await lock.release()


async def next_pending(db: AsyncSession, ticket_id: int) -> TicketMessage | None:
    return await db.scalar(
        select(TicketMessage)
        .where(
            TicketMessage.ticket_id == ticket_id,
            TicketMessage.source == Source.WEB,
            TicketMessage.delivery_status == DeliveryStatus.PENDING,
        )
        .order_by(TicketMessage.id)
        .limit(1)
    )


async def send(
    db: AsyncSession,
    discord_client: discord.Client,
    ticket: Ticket,
    message: TicketMessage,
    content: str,
) -> discord.WebhookMessage:
    """用作者的名稱與頭像送出。webhook 被刪除的話（例如管理員在頻道設定裡刪掉）建立新的。"""
    row = await db.get(TicketUser, (ticket.id, message.author_id))
    name = webhook_name(row)
    avatar = avatar_url(row, message.author_id)
    if ticket.webhook_id is None or ticket.webhook_token is None:
        await recreate_webhook(db, discord_client, ticket)
    try:
        return await post(webhook_for(discord_client, ticket), content, name, avatar)
    except discord.NotFound:
        await recreate_webhook(db, discord_client, ticket)
        return await post(webhook_for(discord_client, ticket), content, name, avatar)
    except discord.HTTPException as error:
        if error.status != 400 or "username" not in error.text:
            raise
        log.warning("Discord rejected the webhook name %r, using the fallback name", name)
        return await post(webhook_for(discord_client, ticket), content, FALLBACK_NAME, avatar)


async def post(
    webhook: discord.Webhook, content: str, name: str, avatar: str
) -> discord.WebhookMessage:
    return await webhook.send(
        content,
        username=name,
        avatar_url=avatar,
        # 網頁訊息裡的 @everyone、<@id> 只顯示，不通知任何人
        allowed_mentions=discord.AllowedMentions.none(),
        wait=True,
    )


async def recreate_webhook(
    db: AsyncSession, discord_client: discord.Client, ticket: Ticket
) -> None:
    assert ticket.channel_id is not None
    try:
        channel = await discord_client.fetch_channel(ticket.channel_id)
    except discord.NotFound as error:
        raise ChannelGone from error
    if not isinstance(channel, discord.TextChannel):
        raise ChannelGone
    webhook = await channel.create_webhook(name="客服單", reason=f"客服單 #{ticket.id}")
    # 先存起來：on_message 用它略過這個 webhook 送出的訊息
    ticket.webhook_id = webhook.id
    ticket.webhook_token = webhook.token
    await db.commit()


async def set_status(db: AsyncSession, message: TicketMessage, status: DeliveryStatus) -> None:
    message.delivery_status = status
    db.add(
        TicketEvent(
            ticket_id=message.ticket_id,
            type="message.updated",
            data={"message_id": str(message.id), "delivery_status": status.value},
        )
    )
    await db.commit()
