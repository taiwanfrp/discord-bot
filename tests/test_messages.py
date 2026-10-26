import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import discord
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db.models import (
    AttachmentStatus,
    Source,
    Ticket,
    TicketAttachment,
    TicketCategory,
    TicketEvent,
    TicketMessage,
    TicketUser,
)
from bot.tickets.messages import save_discord_message
from tests.api.helpers import make_member
from tests.database import create_test_engine


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = await create_test_engine(tmp_path)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


async def make_ticket(db: AsyncSession) -> Ticket:
    category = TicketCategory(name="帳務")
    db.add(category)
    await db.flush()
    ticket = Ticket(category_id=category.id, source=Source.WEB, opener_id=5, subject="", content="")
    ticket.inactivity_reminded_at = datetime(2026, 1, 1, tzinfo=UTC)
    db.add(ticket)
    await db.commit()
    return ticket


def make_message(message_id: int, attachments: list[Any]) -> discord.Message:
    return cast(
        discord.Message,
        SimpleNamespace(
            id=message_id,
            author=make_member(5, []),
            content="你好",
            created_at=datetime(2026, 10, 24, 12, 0, tzinfo=UTC),
            attachments=attachments,
        ),
    )


def make_attachment(attachment_id: int, filename: str, size: int = 10) -> SimpleNamespace:
    return SimpleNamespace(
        id=attachment_id,
        filename=filename,
        size=size,
        url=f"https://cdn.discordapp.com/{filename}",
        content_type="image/png",
    )


async def test_save_message(db: AsyncSession, published_events: AsyncMock) -> None:
    ticket = await make_ticket(db)
    message = make_message(
        100,
        [
            make_attachment(1, "a.png"),
            make_attachment(2, "virus.exe"),
            make_attachment(3, "huge.png", size=2 * 1024**3),
        ],
    )
    pending = await save_discord_message(db, ticket, message)

    saved = await db.scalar(select(TicketMessage))
    assert saved is not None and saved.content == "你好"
    raw = (await db.execute(text("SELECT content FROM ticket_message"))).scalar_one()
    assert raw != "你好"
    assert ticket.last_message_at == message.created_at
    assert ticket.inactivity_reminded_at is None
    assert await db.get(TicketUser, (ticket.id, 5)) is not None

    statuses = {a.filename: a.status for a in await db.scalars(select(TicketAttachment))}
    assert statuses == {
        "a.png": AttachmentStatus.PENDING,
        "virus.exe": AttachmentStatus.REJECTED,
        "huge.png": AttachmentStatus.REJECTED,
    }
    assert len(pending) == 1

    # commit 之後自動推送 message.created
    event = await db.scalar(select(TicketEvent))
    assert event is not None and event.type == "message.created"
    await asyncio.sleep(0)
    published_events.assert_awaited_once_with(event.id)


async def test_duplicate_message_is_ignored(db: AsyncSession) -> None:
    ticket = await make_ticket(db)
    await save_discord_message(db, ticket, make_message(100, []))
    assert await save_discord_message(db, ticket, make_message(100, [])) == []
    assert await db.scalar(select(func.count()).select_from(TicketMessage)) == 1
