from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from fakeredis import FakeAsyncRedis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db.base import Base
from bot.db.engine import create_engine
from bot.db.models import (
    Source,
    Ticket,
    TicketCategory,
    TicketEvent,
    TicketStatus,
    TicketUser,
)
from bot.db.types import utcnow
from bot.tickets import notifications

OPENER, STAFF = 5, 6


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        category = TicketCategory(name="帳務")
        session.add(category)
        await session.flush()
        session.add(
            Ticket(
                category_id=category.id,
                source=Source.WEB,
                status=TicketStatus.CLOSED,
                opener_id=OPENER,
                claimed_by_id=STAFF,
                subject="主旨",
                content="",
                closed_at=utcnow(),
                closed_by_id=STAFF,
                close_reason="已解決",
            )
        )
        session.add(TicketUser(ticket_id=1, user_id=STAFF, username="staff", nickname="客服小明"))
        await session.commit()
        yield session
    await engine.dispose()


class FakeDiscord:
    """fetch_user 回傳的使用者，send 可以設定成失敗。"""

    def __init__(self) -> None:
        self.users: dict[int, MagicMock] = {}

    def user(self, user_id: int) -> MagicMock:
        if user_id not in self.users:
            self.users[user_id] = MagicMock(id=user_id, send=AsyncMock())
        return self.users[user_id]

    async def fetch_user(self, user_id: int) -> MagicMock:
        return self.user(user_id)


def http_error(cls: type[discord.HTTPException], status: int, code: int = 0) -> Any:
    return cls(MagicMock(status=status, reason=""), {"code": code, "message": "error"})


async def run(db: AsyncSession, client: FakeDiscord, *, last_attempt: bool = False) -> None:
    try:
        await notifications.send_close_notifications(
            db, FakeAsyncRedis(), cast(discord.Client, client), 1, last_attempt=last_attempt
        )
    except Exception:
        # 跟 worker 一樣：工作失敗時 session 關閉，沒有 commit 的資料不會留下
        await db.rollback()
        raise


async def recorded(db: AsyncSession) -> list[tuple[str, Any]]:
    rows = await db.scalars(
        select(TicketEvent)
        .where(TicketEvent.type.startswith("notification."))
        .order_by(TicketEvent.id)
    )
    return [(row.type, row.data) for row in rows]


async def test_sends_to_opener_and_claimer(db: AsyncSession) -> None:
    client = FakeDiscord()
    await run(db, client)

    opener = client.user(OPENER).send.call_args.kwargs
    assert opener["embed"].title == "客服單 #1 已關閉"
    fields = {field.name: field.value for field in opener["embed"].fields}
    assert fields == {"分類": "帳務", "關閉者": "客服小明", "主旨": "主旨", "關閉原因": "已解決"}
    link, rate = opener["view"].children
    assert link.url == "https://web.test/tickets/1"
    assert rate.custom_id == "ticket:rate:1"
    # 接手的人只有紀錄的連結
    staff = client.user(STAFF).send.call_args.kwargs
    assert [item.url for item in staff["view"].children] == ["https://web.test/tickets/1"]

    assert await recorded(db) == [
        ("notification.sent", {"user_id": str(OPENER)}),
        ("notification.sent", {"user_id": str(STAFF)}),
    ]
    ticket = await db.get_one(Ticket, 1)
    assert ticket.close_notified_at is not None
    # 重複執行不會再送
    await run(db, client)
    client.user(OPENER).send.assert_awaited_once()


async def test_permanent_failure_is_recorded(db: AsyncSession) -> None:
    client = FakeDiscord()
    # 關閉私訊
    client.user(OPENER).send.side_effect = http_error(discord.Forbidden, 403, 50007)
    await run(db, client)
    assert await recorded(db) == [
        ("notification.failed", {"user_id": str(OPENER), "code": "50007"}),
        ("notification.sent", {"user_id": str(STAFF)}),
    ]


async def test_temporary_failure_retries(db: AsyncSession) -> None:
    client = FakeDiscord()
    client.user(STAFF).send.side_effect = http_error(discord.DiscordServerError, 500)
    with pytest.raises(discord.DiscordServerError):
        await run(db, client)
    assert (await db.get_one(Ticket, 1)).close_notified_at is None

    # 重試時不會再送給已經送過的開單者
    with pytest.raises(discord.DiscordServerError):
        await run(db, client)
    client.user(OPENER).send.assert_awaited_once()

    # 最後一次也失敗：記錄下來，不再重試
    await run(db, client, last_attempt=True)
    assert await recorded(db) == [
        ("notification.sent", {"user_id": str(OPENER)}),
        ("notification.failed", {"user_id": str(STAFF), "code": "0"}),
    ]
    assert (await db.get_one(Ticket, 1)).close_notified_at is not None


async def test_rate_limit_is_temporary() -> None:
    assert not notifications.is_permanent(http_error(discord.HTTPException, 429))
    assert notifications.is_permanent(http_error(discord.NotFound, 404))
    assert not notifications.is_permanent(TimeoutError())


async def test_open_ticket_is_skipped(db: AsyncSession) -> None:
    ticket = await db.get_one(Ticket, 1)
    ticket.status = TicketStatus.OPEN
    await db.commit()
    client = FakeDiscord()
    await run(db, client)
    assert client.users == {}


async def test_unclaimed_ticket(db: AsyncSession) -> None:
    ticket = await db.get_one(Ticket, 1)
    ticket.claimed_by_id = None
    ticket.closed_by_id = None
    await db.commit()
    client = FakeDiscord()
    await run(db, client)
    assert list(client.users) == [OPENER]
    fields = {f.name: f.value for f in client.user(OPENER).send.call_args.kwargs["embed"].fields}
    assert fields["關閉者"] == "系統"


async def test_busy(db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notifications, "LOCK_WAIT", 0.1)
    redis = FakeAsyncRedis()
    await redis.set("ticket-bot:lock:close_notifications:1", "other")
    with pytest.raises(notifications.NotificationsBusy):
        await notifications.send_close_notifications(
            db, redis, cast(discord.Client, FakeDiscord()), 1, last_attempt=True
        )
