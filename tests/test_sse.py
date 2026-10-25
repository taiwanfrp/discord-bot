import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.api.deps import Viewer
from bot.api.routes.events import event_stream
from bot.api.sse import EventHub
from bot.core.permissions import NO_ACCESS
from bot.db.base import Base
from bot.db.engine import create_engine
from bot.db.models import Source, Ticket, TicketCategory, TicketEvent

OPENER = 5


@pytest.fixture
async def maker(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        category = TicketCategory(name="帳務")
        db.add(category)
        await db.flush()
        for opener in (OPENER, 6):
            db.add(
                Ticket(
                    category_id=category.id,
                    source=Source.WEB,
                    opener_id=opener,
                    subject="",
                    content="",
                )
            )
        await db.commit()
    yield maker
    await engine.dispose()


async def add_event(maker: async_sessionmaker[AsyncSession], ticket_id: int, type: str) -> int:
    async with maker() as db:
        event = TicketEvent(ticket_id=ticket_id, type=type)
        db.add(event)
        await db.commit()
        return event.id


def viewer() -> Viewer:
    return Viewer(user=cast(Any, SimpleNamespace(id=OPENER)), member=None, access=NO_ACCESS)


async def ok() -> Viewer:
    return viewer()


async def take(stream: AsyncIterator[Any], count: int) -> list[Any]:
    return [await asyncio.wait_for(anext(stream), 1) for _ in range(count)]


async def test_ticket_stream_with_replay(maker: async_sessionmaker[AsyncSession]) -> None:
    hub = EventHub(lambda: maker)
    first = await add_event(maker, 1, "ticket.created")
    second = await add_event(maker, 1, "message.created")
    await add_event(maker, 2, "message.created")  # 別張單

    stream = event_stream(hub=hub, sessionmaker=maker, ticket_id=1, last_event_id=first, check=ok)
    # 補送 last_event_id 之後的事件
    (replayed,) = await take(stream, 1)
    assert (replayed.id, replayed.event) == (str(second), "message.created")
    assert replayed.data["ticket_id"] == 1

    # 補送過的事件再收到一次（補送期間同時推送）不會重複
    hub.publish(second)
    third = await add_event(maker, 1, "ticket.claimed")
    hub.publish(third)
    (live,) = await take(stream, 1)
    assert live.id == str(third)
    hub.close()
    with pytest.raises(StopAsyncIteration):
        await take(stream, 1)


async def test_out_of_order_events_are_delivered(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    hub = EventHub(lambda: maker)
    stream = event_stream(hub=hub, sessionmaker=maker, ticket_id=1, last_event_id=None, check=ok)
    pending = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0)  # 讓 stream 先訂閱
    first = await add_event(maker, 1, "message.created")
    second = await add_event(maker, 1, "attachment.updated")
    # 比較小的 id 比較晚 commit（兩個交易同時寫入）
    hub.publish(second)
    hub.publish(first)
    events = [await asyncio.wait_for(pending, 1), *await take(stream, 1)]
    assert [e.id for e in events] == [str(second), str(first)]
    hub.close()


async def test_list_stream_filters(maker: async_sessionmaker[AsyncSession]) -> None:
    hub = EventHub(lambda: maker)
    stream = event_stream(hub=hub, sessionmaker=maker, ticket_id=None, last_event_id=0, check=ok)
    mine = await add_event(maker, 1, "ticket.created")
    await add_event(maker, 2, "ticket.created")  # 看不到別人的單
    await add_event(maker, 1, "message.created")  # 列表不送訊息事件
    latest = await add_event(maker, 1, "ticket.closed")
    events = await take(stream, 2)
    assert [e.id for e in events] == [str(mine), str(latest)]


async def test_stream_ends_when_session_expires(maker: async_sessionmaker[AsyncSession]) -> None:
    calls = 0

    async def check() -> Viewer:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise HTTPException(401)
        return viewer()

    stream = event_stream(
        hub=EventHub(lambda: maker),
        sessionmaker=maker,
        ticket_id=1,
        last_event_id=None,
        check=check,
        interval=0.01,
    )
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(stream), 1)
