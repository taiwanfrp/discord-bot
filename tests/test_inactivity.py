from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db.models import GuildSettings, Source, Ticket, TicketCategory, TicketEvent, TicketStatus
from bot.db.types import utcnow
from bot.tickets import inactivity
from tests.api.helpers import FakeGuild, make_text_channel
from tests.database import create_test_engine

OPENER = 5
DAY = timedelta(days=1)


@pytest.fixture
async def sessionmaker(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = await create_test_engine(tmp_path)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        db.add(TicketCategory(name="帳務"))
        await db.commit()
    yield maker
    await engine.dispose()


class Env:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self.sessionmaker = sessionmaker
        self.guild = FakeGuild()
        self.client = cast(
            discord.Client, SimpleNamespace(fetch_guild=AsyncMock(return_value=self.guild))
        )

    async def ticket(
        self,
        *,
        idle: timedelta,
        reminded: timedelta | None = None,
        status: TicketStatus = TicketStatus.OPEN,
        channel: bool = True,
        opener_added: bool = True,
    ) -> tuple[int, Any]:
        """idle：最後一則訊息是多久以前；reminded：多久以前提醒過。"""
        now = utcnow()
        async with self.sessionmaker() as db:
            ticket = Ticket(
                category_id=1,
                source=Source.DISCORD,
                status=status,
                opener_id=OPENER,
                opener_added_at=now - 10 * DAY if opener_added else None,
                subject="",
                content="",
                last_message_at=now - idle,
                inactivity_reminded_at=now - reminded if reminded is not None else None,
            )
            db.add(ticket)
            await db.flush()
            ticket.channel_id = 900 + ticket.id
            await db.commit()
        text_channel = make_text_channel(900 + ticket.id)
        text_channel.send = AsyncMock(return_value=MagicMock(delete=AsyncMock()))
        if channel:
            self.guild.channels.append(text_channel)
        return ticket.id, text_channel

    async def check(self) -> None:
        async with self.sessionmaker() as db:
            await inactivity.check_inactive_tickets(db, self.client)

    async def get(self, ticket_id: int) -> Ticket:
        async with self.sessionmaker() as db:
            return await db.get_one(Ticket, ticket_id)

    async def events(self, type: str) -> list[int]:
        async with self.sessionmaker() as db:
            rows = await db.scalars(select(TicketEvent).where(TicketEvent.type == type))
            return [row.ticket_id for row in rows]


@pytest.fixture
def env(sessionmaker: async_sessionmaker[AsyncSession]) -> Env:
    return Env(sessionmaker)


async def set_hours(db: AsyncSession, remind: int | None, close: int | None) -> None:
    # 新增時傳 None 會變成欄位的預設值，所以先建立再修改（跟 PATCH /settings 一樣）
    db.add(GuildSettings(id=1))
    await db.flush()
    await db.execute(
        update(GuildSettings).values(
            inactivity_remind_after_hours=remind, inactivity_close_after_hours=close
        )
    )
    await db.commit()


async def test_remind(env: Env) -> None:
    stale, channel = await env.ticket(idle=5 * DAY)
    fresh, fresh_channel = await env.ticket(idle=3 * DAY)
    await env.check()

    kwargs = channel.send.call_args.kwargs
    assert channel.send.call_args.args == (f"<@{OPENER}>",)
    assert kwargs["allowed_mentions"].users == [discord.Object(OPENER)]
    assert kwargs["embed"].title == "這張客服單已經超過 4 天沒有新訊息"
    assert "自動關閉" in kwargs["embed"].description
    [button] = kwargs["view"].children
    # 由 bot 的 TicketControlsView 處理（跟開單訊息的「關閉」一樣）
    assert (button.label, button.custom_id) == ("立即關閉", "ticket:close")
    fresh_channel.send.assert_not_awaited()
    assert (await env.get(stale)).inactivity_reminded_at is not None
    assert await env.events("ticket.inactivity_reminded") == [stale]

    # 已經提醒過就不會再提醒
    await env.check()
    channel.send.assert_awaited_once()


async def test_auto_close(env: Env, close_notifications: AsyncMock) -> None:
    due, channel = await env.ticket(idle=6 * DAY, reminded=2 * DAY)
    waiting, _ = await env.ticket(idle=5 * DAY, reminded=timedelta(hours=2))
    # 提醒之後有新訊息（正常情況下 inactivity_reminded_at 會被清除）
    talked, _ = await env.ticket(idle=timedelta(hours=1), reminded=2 * DAY)
    closed, _ = await env.ticket(idle=10 * DAY, reminded=5 * DAY, status=TicketStatus.CLOSED)
    await env.check()

    ticket = await env.get(due)
    assert ticket.status is TicketStatus.CLOSED
    assert ticket.closed_by_id is None
    assert ticket.close_reason == "提醒後 1 天沒有新訊息，自動關閉"
    channel.delete.assert_awaited_once()
    close_notifications.assert_awaited_once_with(due)
    for ticket_id in (waiting, talked):
        assert (await env.get(ticket_id)).status is TicketStatus.OPEN
    assert await env.events("ticket.closed") == [due]


async def test_disabled(env: Env) -> None:
    async with env.sessionmaker() as db:
        await set_hours(db, None, None)
    _, channel = await env.ticket(idle=30 * DAY)
    reminded, _ = await env.ticket(idle=30 * DAY, reminded=10 * DAY)
    await env.check()
    channel.send.assert_not_awaited()
    assert (await env.get(reminded)).status is TicketStatus.OPEN


async def test_remind_without_auto_close(env: Env) -> None:
    async with env.sessionmaker() as db:
        await set_hours(db, 36, None)
    _, channel = await env.ticket(idle=2 * DAY, opener_added=False)
    await env.check()
    kwargs = channel.send.call_args.kwargs
    assert kwargs["embed"].title == "這張客服單已經超過 36 小時沒有新訊息"
    assert "自動關閉" not in kwargs["embed"].description
    # 開單者不在頻道裡，不提及
    assert channel.send.call_args.args == (None,)


async def test_message_while_reminding(env: Env) -> None:
    ticket_id, channel = await env.ticket(idle=5 * DAY)

    async def new_message(*_: Any, **__: Any) -> Any:
        # 送出提醒的同時，有人傳了訊息
        async with env.sessionmaker() as db:
            await db.execute(
                update(Ticket).where(Ticket.id == ticket_id).values(last_message_at=utcnow())
            )
            await db.commit()
        return reminder

    reminder = MagicMock(delete=AsyncMock())
    channel.send = AsyncMock(side_effect=new_message)
    await env.check()
    reminder.delete.assert_awaited_once()
    assert (await env.get(ticket_id)).inactivity_reminded_at is None
    assert await env.events("ticket.inactivity_reminded") == []


async def test_channel_gone(env: Env) -> None:
    ticket_id, channel = await env.ticket(idle=5 * DAY, channel=False)
    await env.check()
    ticket = await env.get(ticket_id)
    assert (ticket.status, ticket.closed_by_id) == (TicketStatus.CLOSED, None)
    channel.send.assert_not_awaited()


async def test_one_failure_does_not_stop_others(env: Env) -> None:
    failing, channel = await env.ticket(idle=6 * DAY)
    channel.send.side_effect = discord.DiscordServerError(MagicMock(status=500), "boom")
    other, _ = await env.ticket(idle=5 * DAY)
    await env.check()
    assert (await env.get(failing)).inactivity_reminded_at is None
    assert (await env.get(other)).inactivity_reminded_at is not None


def test_duration_text() -> None:
    assert inactivity.duration_text(96) == "4 天"
    assert inactivity.duration_text(36) == "36 小時"


async def test_message_before_auto_close(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    ticket_id, channel = await env.ticket(idle=6 * DAY, reminded=2 * DAY)
    query = inactivity.open_ticket_ids

    async def then_new_message(*args: Any) -> list[int]:
        ids = await query(*args)
        # 查詢之後、關單之前，有人傳了訊息（儲存訊息時會清除 inactivity_reminded_at）
        async with env.sessionmaker() as db:
            await db.execute(
                update(Ticket)
                .where(Ticket.id == ticket_id)
                .values(last_message_at=utcnow(), inactivity_reminded_at=None)
            )
            await db.commit()
        return ids

    monkeypatch.setattr(inactivity, "open_ticket_ids", then_new_message)
    await env.check()
    assert (await env.get(ticket_id)).status is TicketStatus.OPEN
    channel.delete.assert_not_awaited()
