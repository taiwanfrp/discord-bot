"""閒置提醒與自動關閉（docs/tickets.md 第 9 節）。由 worker 的 check_inactive_tickets 執行。"""

import contextlib
import logging
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

import discord
from sqlalchemy import ColumnElement, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from bot.core.config import get_settings
from bot.db.models import Ticket, TicketStatus
from bot.db.queries import execute_update, get_guild_settings
from bot.db.types import utcnow
from bot.tickets import actions
from bot.tickets.channels import get_text_channel
from bot.tickets.controls import CLOSE_CUSTOM_ID

log = logging.getLogger(__name__)

# 每次最多處理幾張，剩下的下一次（5 分鐘後）再處理
BATCH_SIZE = 50


def duration_text(hours: int) -> str:
    return f"{hours // 24} 天" if hours % 24 == 0 else f"{hours} 小時"


def reminder_embed(remind_after: int, close_at: datetime | None) -> discord.Embed:
    lines = ["問題已經解決的話，可以按下面的「立即關閉」。"]
    if close_at is not None:
        timestamp = int(close_at.timestamp())
        lines.append(
            f"在 <t:{timestamp}:f>（<t:{timestamp}:R>）之前沒有新訊息的話，客服單會自動關閉。"
        )
    return discord.Embed(
        title=f"這張客服單已經超過 {duration_text(remind_after)}沒有新訊息",
        description="\n".join(lines),
        color=discord.Color.orange(),
    )


def reminder_view() -> discord.ui.View:
    # 跟開單訊息的「關閉」是同一個 custom_id，由 bot 的 TicketControlsView 處理，一樣要確認
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="立即關閉", style=discord.ButtonStyle.danger, custom_id=CLOSE_CUSTOM_ID
        )
    )
    return view


async def check_inactive_tickets(db: AsyncSession, discord_client: discord.Client) -> None:
    """先關閉提醒過、時間到的單，再提醒新的閒置的單。

    一張單失敗不影響其他的單；這是定時執行的掃描，下一次會再處理。
    """
    settings = await get_guild_settings(db)
    # rollback 之後 ORM 物件會過期，先把需要的值取出來
    remind_after = settings.inactivity_remind_after_hours
    close_after = settings.inactivity_close_after_hours
    # 還沒有設定時會新增一筆；先結束交易，等 Discord 回應的期間才不會佔著資料庫
    await db.commit()
    now = utcnow()

    due: list[ColumnElement[bool]] = []
    if close_after is not None:
        due = [
            Ticket.inactivity_reminded_at < now - timedelta(hours=close_after),
            # 有新訊息時會清除 inactivity_reminded_at；這裡再確認一次
            Ticket.last_message_at <= Ticket.inactivity_reminded_at,
        ]
    stale: list[ColumnElement[bool]] = []
    if remind_after is not None:
        stale = [
            Ticket.inactivity_reminded_at.is_(None),
            Ticket.last_message_at < now - timedelta(hours=remind_after),
        ]
    close_ids = await open_ticket_ids(db, due, Ticket.inactivity_reminded_at) if due else []
    remind_ids = await open_ticket_ids(db, stale, Ticket.last_message_at) if stale else []
    if not close_ids and not remind_ids:
        return

    guild = await discord_client.fetch_guild(get_settings().guild_id)
    for ticket_id in close_ids:
        assert close_after is not None
        try:
            await close(db, guild, ticket_id, close_after, due)
        except Exception:
            await db.rollback()
            log.exception("Failed to close inactive ticket %d", ticket_id)
    for ticket_id in remind_ids:
        assert remind_after is not None
        try:
            await remind(db, guild, ticket_id, stale, remind_after, close_after, now)
        except Exception:
            await db.rollback()
            log.exception("Failed to remind inactive ticket %d", ticket_id)


async def open_ticket_ids(
    db: AsyncSession, conditions: Sequence[ColumnElement[bool]], order: InstrumentedAttribute[Any]
) -> list[int]:
    return list(
        await db.scalars(
            select(Ticket.id)
            .where(Ticket.status == TicketStatus.OPEN, *conditions)
            .order_by(order)
            .limit(BATCH_SIZE)
        )
    )


async def close(
    db: AsyncSession,
    guild: discord.Guild,
    ticket_id: int,
    close_after: int,
    due: Sequence[ColumnElement[bool]],
) -> None:
    ticket = await db.get_one(Ticket, ticket_id)
    reason = f"提醒後 {duration_text(close_after)}沒有新訊息，自動關閉"
    # 關單的當下再確認一次條件：查詢之後可能剛好有新訊息
    await actions.close(db, guild, ticket, None, reason, conditions=due)


async def remind(
    db: AsyncSession,
    guild: discord.Guild,
    ticket_id: int,
    stale: Sequence[ColumnElement[bool]],
    remind_after: int,
    close_after: int | None,
    now: datetime,
) -> None:
    ticket = await db.get_one(Ticket, ticket_id)
    channel = await get_text_channel(guild, ticket.channel_id) if ticket.channel_id else None
    if channel is None:
        # 頻道已經不存在（bot 離線時被刪除）：跟 on_guild_channel_delete 一樣，由系統關單
        await actions.close(db, guild, ticket, None, None, delete_channel=False)
        return

    # 先送出再記錄：記錄之前中斷的話，下一次會再提醒一次，但不會沒有提醒就自動關閉
    view = reminder_view()
    close_at = now + timedelta(hours=close_after) if close_after is not None else None
    message = await channel.send(
        # 開單者還沒加進頻道（不在伺服器裡）時，網頁上一樣看得到 ticket.inactivity_reminded
        f"<@{ticket.opener_id}>" if ticket.opener_added_at is not None else None,
        embed=reminder_embed(remind_after, close_at),
        view=view,
        allowed_mentions=discord.AllowedMentions(users=[discord.Object(ticket.opener_id)]),
    )
    # 按鈕由 bot 處理；worker 不需要把這個 view 留在記憶體裡
    view.stop()

    reminded = await execute_update(
        db,
        update(Ticket)
        .where(Ticket.id == ticket.id, Ticket.status == TicketStatus.OPEN, *stale)
        .values(inactivity_reminded_at=now),
    )
    if not reminded:
        # 送出的同時有新訊息（或客服單剛好被關閉）：已經不需要這個提醒
        await db.rollback()
        with contextlib.suppress(discord.HTTPException):
            await message.delete()
        return
    actions.add_event(db, ticket, "ticket.inactivity_reminded", None)
    await db.commit()
