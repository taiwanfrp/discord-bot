from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db.base import Base
from bot.db.engine import create_engine
from bot.db.models import Source, Ticket, TicketCategory, TicketStatus


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


async def add_ticket(session: AsyncSession) -> Ticket:
    category = TicketCategory(name="帳務")
    session.add(category)
    await session.flush()
    ticket = Ticket(
        category_id=category.id,
        source=Source.DISCORD,
        opener_id=2**63 - 1,
        subject="主旨",
        content="內容",
    )
    session.add(ticket)
    await session.commit()
    return ticket


async def test_encrypted_columns(session: AsyncSession) -> None:
    ticket = await add_ticket(session)

    raw = (await session.execute(text("SELECT subject FROM ticket"))).scalar_one()
    assert "主旨" not in raw

    session.expunge_all()
    loaded = (await session.execute(select(Ticket))).scalar_one()
    assert loaded.id == ticket.id
    assert loaded.subject == "主旨"
    assert loaded.opener_id == 2**63 - 1


async def test_enum_stored_as_value(session: AsyncSession) -> None:
    await add_ticket(session)

    raw = (await session.execute(text("SELECT status, source FROM ticket"))).one()
    assert tuple(raw) == ("open", "discord")

    session.expunge_all()
    loaded = (await session.execute(select(Ticket))).scalar_one()
    assert loaded.status is TicketStatus.OPEN


async def test_datetime_is_utc(session: AsyncSession) -> None:
    ticket = await add_ticket(session)
    session.expunge_all()

    loaded = await session.get_one(Ticket, ticket.id)
    assert loaded.created_at.tzinfo is UTC


async def test_naive_datetime_rejected(session: AsyncSession) -> None:
    ticket = await add_ticket(session)
    ticket.closed_at = datetime(2026, 1, 1)
    with pytest.raises(StatementError, match="時區"):
        await session.commit()


async def test_sqlite_foreign_keys(session: AsyncSession) -> None:
    session.add(Ticket(category_id=999, source=Source.WEB, opener_id=1, subject="", content=""))
    with pytest.raises(IntegrityError):
        await session.commit()
