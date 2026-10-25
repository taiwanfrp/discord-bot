"""關單後的私訊（docs/tickets.md 第 10 節）。由 worker 的 send_close_notifications 執行。"""

import contextlib
import logging

import discord
from redis.asyncio import Redis
from redis.exceptions import LockError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Ticket, TicketCategory, TicketEvent, TicketStatus, TicketUser
from bot.db.types import utcnow
from bot.tickets.actions import add_event
from bot.tickets.rating import closed_view
from bot.tickets.snapshot import display_name

log = logging.getLogger(__name__)

# 每個收件人送出（或確定送不出去）後各記錄一筆，重新執行時就不會再送一次
SENT = "notification.sent"
FAILED = "notification.failed"

LOCK_TIMEOUT = 60
LOCK_WAIT = 10


class NotificationsBusy(Exception):
    """另一個 worker 正在送這張單的私訊，稍後重試。"""


def recipients(ticket: Ticket) -> list[tuple[int, bool]]:
    """(user_id, 有沒有評分按鈕)：開單者，以及接手的人（如果有）。"""
    result = [(ticket.opener_id, True)]
    if ticket.claimed_by_id is not None and ticket.claimed_by_id != ticket.opener_id:
        result.append((ticket.claimed_by_id, False))
    return result


def closed_embed(
    ticket: Ticket, category_name: str, closer: TicketUser | None, *, for_opener: bool
) -> discord.Embed:
    embed = discord.Embed(
        title=f"客服單 #{ticket.id} 已關閉",
        description="歡迎為這次的客服評分。" if for_opener else None,
        color=discord.Color.red(),
        timestamp=ticket.closed_at,
    )
    embed.add_field(name="分類", value=category_name)
    # 私訊裡的 <@id> 不一定顯示得出名稱，所以用關單當時的快照
    embed.add_field(name="關閉者", value=display_name(closer) if closer else "系統")
    embed.add_field(name="主旨", value=ticket.subject, inline=False)
    if ticket.close_reason:
        embed.add_field(name="關閉原因", value=ticket.close_reason[:1024], inline=False)
    return embed


def is_permanent(error: Exception) -> bool:
    """關閉私訊、沒有共同的伺服器、帳號已刪除等（4xx）：重試也一樣。429 是暫時的。"""
    return (
        isinstance(error, discord.HTTPException)
        and 400 <= error.status < 500
        and error.status != 429
    )


async def send_close_notifications(
    db: AsyncSession,
    redis: Redis,
    discord_client: discord.Client,
    ticket_id: int,
    *,
    last_attempt: bool,
) -> None:
    lock = redis.lock(
        f"ticket-bot:lock:close_notifications:{ticket_id}",
        timeout=LOCK_TIMEOUT,
        blocking_timeout=LOCK_WAIT,
    )
    if not await lock.acquire():
        raise NotificationsBusy(ticket_id)
    try:
        await send_all(db, discord_client, ticket_id, last_attempt=last_attempt)
    finally:
        with contextlib.suppress(LockError):
            await lock.release()


async def send_all(
    db: AsyncSession, discord_client: discord.Client, ticket_id: int, *, last_attempt: bool
) -> None:
    ticket = await db.get(Ticket, ticket_id)
    if (
        ticket is None
        or ticket.status is not TicketStatus.CLOSED
        or ticket.close_notified_at is not None
    ):
        return
    handled = await handled_recipients(db, ticket.id)
    category = await db.get(TicketCategory, ticket.category_id)
    closer = (
        await db.get(TicketUser, (ticket.id, ticket.closed_by_id))
        if ticket.closed_by_id is not None
        else None
    )
    for user_id, rating in recipients(ticket):
        if user_id in handled:
            continue
        embed = closed_embed(
            ticket, category.name if category else "（已刪除）", closer, for_opener=rating
        )
        try:
            user = await discord_client.fetch_user(user_id)
            await user.send(embed=embed, view=closed_view(ticket.id, rating=rating))
        except Exception as error:
            if not is_permanent(error) and not last_attempt:
                raise
            # 不重試；使用者在網頁上一樣可以看到紀錄、評分
            log.info("Failed to send the close notification of ticket %d to %d", ticket.id, user_id)
            code = error.code if isinstance(error, discord.HTTPException) else None
            add_event(db, ticket, FAILED, None, user_id=str(user_id), code=str(code))
        else:
            add_event(db, ticket, SENT, None, user_id=str(user_id))
        # 每送一個就記錄，中途失敗重試時不會重複送給前面的人
        await db.commit()
    ticket.close_notified_at = utcnow()
    await db.commit()


async def handled_recipients(db: AsyncSession, ticket_id: int) -> set[int]:
    events = await db.scalars(
        select(TicketEvent).where(
            TicketEvent.ticket_id == ticket_id, TicketEvent.type.in_((SENT, FAILED))
        )
    )
    return {int(event.data["user_id"]) for event in events if event.data}
