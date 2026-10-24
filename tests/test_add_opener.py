from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db.base import Base
from bot.db.engine import create_engine
from bot.db.models import Source, Ticket, TicketCategory, TicketEvent
from bot.tickets.open import add_opener, pending_openers_query
from tests.api.helpers import make_member, make_text_channel


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


async def test_add_opener_only_once(db: AsyncSession) -> None:
    category = TicketCategory(name="帳務")
    db.add(category)
    await db.flush()
    ticket = Ticket(
        category_id=category.id,
        source=Source.WEB,
        opener_id=5,
        channel_id=900,
        subject="",
        content="",
    )
    db.add(ticket)
    await db.commit()
    assert [t.id for t in await db.scalars(pending_openers_query(5))] == [ticket.id]

    channel, member = make_text_channel(900), make_member(5, [])
    assert await add_opener(db, ticket, channel, member) is not None
    # 第二次（例如 on_member_join 和 sync_pending_openers 同時處理）不會重複通知
    assert await add_opener(db, ticket, channel, member) is None

    channel.send.assert_awaited_once()
    assert await db.scalar(select(func.count()).select_from(TicketEvent)) == 1
    assert list(await db.scalars(pending_openers_query(5))) == []
