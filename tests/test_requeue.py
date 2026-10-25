from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, call

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db.base import Base
from bot.db.engine import create_engine
from bot.db.models import (
    DeliveryStatus,
    Source,
    Ticket,
    TicketAttachment,
    TicketCategory,
    TicketMessage,
    TicketStatus,
)
from bot.db.types import utcnow
from bot.worker import tasks

OLD = timedelta(minutes=20)


@pytest.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncSession]:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(tasks, "get_sessionmaker", lambda: sessionmaker)
    async with sessionmaker() as session:
        category = TicketCategory(name="帳務")
        session.add(category)
        await session.flush()
        for _ in range(2):
            session.add(
                Ticket(
                    category_id=category.id, source=Source.WEB, opener_id=5, subject="", content=""
                )
            )
        await session.commit()
        yield session
    await engine.dispose()


def attachment(source: Source, *, age: timedelta, uploaded: timedelta | None) -> TicketAttachment:
    return TicketAttachment(
        ticket_id=1,
        uploader_id=5,
        source=source,
        filename="a.png",
        size=1,
        created_at=utcnow() - age,
        uploaded_at=utcnow() - uploaded if uploaded is not None else None,
    )


async def test_requeue_attachments(db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    mirror, verify = AsyncMock(), AsyncMock()
    monkeypatch.setattr(tasks.mirror_attachment, "kiq", mirror)
    monkeypatch.setattr(tasks.verify_attachment, "kiq", verify)
    rows = [
        attachment(Source.DISCORD, age=OLD, uploaded=None),
        attachment(Source.DISCORD, age=timedelta(), uploaded=None),
        attachment(Source.WEB, age=OLD, uploaded=OLD),
        # 還在上傳（等使用者上傳完，不是等檢查）
        attachment(Source.WEB, age=OLD, uploaded=None),
        # 剛上傳完
        attachment(Source.WEB, age=OLD, uploaded=timedelta()),
    ]
    db.add_all(rows)
    await db.commit()

    await tasks.requeue_pending_attachments()
    mirror.assert_has_awaits([call(rows[0].id)])
    assert mirror.await_count == 1
    verify.assert_has_awaits([call(rows[2].id)])
    assert verify.await_count == 1


async def test_requeue_messages(db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    deliver = AsyncMock()
    monkeypatch.setattr(tasks.deliver_messages, "kiq", deliver)
    for ticket_id, status, age in (
        (1, DeliveryStatus.PENDING, OLD),
        (1, DeliveryStatus.PENDING, OLD),
        (2, DeliveryStatus.SENT, OLD),
        (2, DeliveryStatus.PENDING, timedelta()),
    ):
        db.add(
            TicketMessage(
                ticket_id=ticket_id,
                source=Source.WEB,
                author_id=5,
                content="",
                delivery_status=status,
                created_at=utcnow() - age,
            )
        )
    await db.commit()

    await tasks.requeue_pending_messages()
    # 以客服單為單位，同一張單只放一次
    deliver.assert_awaited_once_with(1)


async def test_requeue_close_notifications(
    db: AsyncSession, close_notifications: AsyncMock
) -> None:
    now = utcnow()
    for status, closed, notified in (
        (TicketStatus.CLOSED, OLD, False),
        # 剛關閉，工作可能還在 queue 裡
        (TicketStatus.CLOSED, timedelta(minutes=2), False),
        # 太久以前
        (TicketStatus.CLOSED, timedelta(days=2), False),
        (TicketStatus.CLOSED, OLD, True),
        (TicketStatus.OPEN, None, False),
    ):
        db.add(
            Ticket(
                category_id=1,
                source=Source.WEB,
                opener_id=5,
                subject="",
                content="",
                status=status,
                closed_at=now - closed if closed is not None else None,
                close_notified_at=now if notified else None,
            )
        )
    await db.commit()

    await tasks.requeue_close_notifications()
    # 前兩張是 fixture 建立的開啟中客服單
    close_notifications.assert_awaited_once_with(3)
