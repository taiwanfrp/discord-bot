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
    DiscordMedia,
    MediaKind,
    Source,
    Ticket,
    TicketAttachment,
    TicketCategory,
    TicketEvent,
    TicketMessage,
    TicketMessageRevision,
    TicketStatus,
    TicketUser,
)
from bot.tickets.messages import (
    delete_discord_messages,
    edit_discord_message,
    save_discord_message,
)
from tests.api.helpers import make_member
from tests.database import create_test_engine


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = await create_test_engine(tmp_path)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


CREATED_AT = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


async def make_ticket(db: AsyncSession) -> Ticket:
    category = TicketCategory(name="帳務")
    db.add(category)
    await db.flush()
    ticket = Ticket(category_id=category.id, source=Source.WEB, opener_id=5, subject="", content="")
    ticket.inactivity_reminded_at = datetime(2026, 1, 1, tzinfo=UTC)
    db.add(ticket)
    await db.commit()
    return ticket


def make_message(
    message_id: int,
    attachments: list[Any],
    content: str = "你好",
    edited_at: datetime | None = None,
    stickers: list[Any] | None = None,
) -> discord.Message:
    return cast(
        discord.Message,
        SimpleNamespace(
            id=message_id,
            author=make_member(5, []),
            content=content,
            created_at=CREATED_AT,
            edited_at=edited_at,
            attachments=attachments,
            stickers=stickers or [],
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


async def test_emojis_and_stickers_are_recorded(db: AsyncSession) -> None:
    ticket = await make_ticket(db)
    stickers = [
        SimpleNamespace(id=300, name="揮手", format=discord.StickerFormatType.apng),
        SimpleNamespace(id=301, name="wumpus", format=discord.StickerFormatType.lottie),
    ]
    content = "<a:dance:200000000000000001> <:ok:200000000000000002> <:ok:200000000000000002>"
    await save_discord_message(db, ticket, make_message(100, [], content, stickers=stickers))

    saved = await db.scalar(select(TicketMessage))
    assert saved is not None and saved.stickers == [
        {"id": "300", "name": "揮手", "format": "apng"},
        {"id": "301", "name": "wumpus", "format": "lottie"},
    ]

    async def recorded() -> set[tuple[int, MediaKind, str]]:
        rows = await db.scalars(select(DiscordMedia))
        return {(row.id, row.kind, row.extension) for row in rows}

    assert await recorded() == {
        (200000000000000001, MediaKind.EMOJI, "gif"),
        (200000000000000002, MediaKind.EMOJI, "webp"),
        (300, MediaKind.STICKER, "png"),
        (301, MediaKind.STICKER, "json"),
    }

    # 編輯後新增的表情也會記錄，已經記錄過的不會重複
    edited_at = datetime(2026, 10, 24, 12, 5, tzinfo=UTC)
    edited = "<:ok:200000000000000002> <:new:200000000000000003>"
    await edit_discord_message(db, make_message(100, [], edited, edited_at))
    assert len(await recorded()) == 5
    assert (200000000000000003, MediaKind.EMOJI, "webp") in await recorded()


async def event_types(db: AsyncSession) -> list[tuple[str, int | None]]:
    events = await db.scalars(select(TicketEvent).order_by(TicketEvent.id))
    return [(event.type, event.actor_id) for event in events]


async def test_edit_message(db: AsyncSession) -> None:
    ticket = await make_ticket(db)
    first, second = make_attachment(1, "a.png"), make_attachment(2, "b.png")
    await save_discord_message(db, ticket, make_message(100, [first, second]))
    edited_at = [datetime(2026, 10, 24, 12, minute, tzinfo=UTC) for minute in (5, 10)]

    # 改內容，並移除第二個附件
    await edit_discord_message(db, make_message(100, [first], "第二版", edited_at[0]))
    # 只改內容
    await edit_discord_message(db, make_message(100, [first], "第三版", edited_at[1]))

    saved = await db.scalar(select(TicketMessage).execution_options(populate_existing=True))
    assert saved is not None
    assert (saved.content, saved.edited_at) == ("第三版", edited_at[1])
    revisions = list(
        await db.scalars(select(TicketMessageRevision).order_by(TicketMessageRevision.id))
    )
    # 每一版的時間範圍接在一起
    assert [(r.content, r.created_at, r.replaced_at) for r in revisions] == [
        ("你好", CREATED_AT, edited_at[0]),
        ("第二版", edited_at[0], edited_at[1]),
    ]
    raw = (await db.execute(text("SELECT content FROM ticket_message_revision"))).scalars()
    assert "你好" not in list(raw)
    # 移除的附件保留，只標記時間
    removed = {
        a.filename: a.removed_at
        for a in await db.scalars(
            select(TicketAttachment).execution_options(populate_existing=True)
        )
    }
    assert removed == {"a.png": None, "b.png": edited_at[0]}
    assert await event_types(db) == [
        ("message.created", 5),
        ("message.updated", 5),
        ("message.updated", 5),
    ]


async def test_edit_without_changes_is_ignored(db: AsyncSession) -> None:
    """例如 Discord 產生連結預覽時的更新：內容、附件都沒變。"""
    ticket = await make_ticket(db)
    attachment = make_attachment(1, "a.png")
    await save_discord_message(db, ticket, make_message(100, [attachment]))
    await edit_discord_message(db, make_message(100, [attachment]))

    saved = await db.scalar(select(TicketMessage))
    assert saved is not None and saved.edited_at is None
    assert await db.scalar(select(func.count()).select_from(TicketMessageRevision)) == 0
    assert await event_types(db) == [("message.created", 5)]


async def test_edit_ignored(db: AsyncSession) -> None:
    ticket = await make_ticket(db)
    # 網頁訊息在 Discord 上的內容多了附件連結，跟資料庫的不一樣，但它不是被編輯
    db.add(
        TicketMessage(
            ticket_id=ticket.id,
            source=Source.WEB,
            discord_message_id=200,
            author_id=5,
            content="網頁",
        )
    )
    await db.commit()
    await edit_discord_message(db, make_message(200, [], "網頁\n📎 [a.png](https://bot.test/a)"))
    # 不是客服單裡的訊息
    await edit_discord_message(db, make_message(300, [], "其他頻道"))
    # 關單後不再更新
    await save_discord_message(db, ticket, make_message(100, []))
    ticket.status = TicketStatus.CLOSED
    await db.commit()
    await edit_discord_message(db, make_message(100, [], "關單後"))

    contents = await db.scalars(select(TicketMessage.content).order_by(TicketMessage.id))
    assert list(contents) == ["網頁", "你好"]
    assert await db.scalar(select(func.count()).select_from(TicketMessageRevision)) == 0


async def test_delete_messages(db: AsyncSession) -> None:
    ticket = await make_ticket(db)
    await save_discord_message(db, ticket, make_message(100, [make_attachment(1, "a.png")]))
    # 網頁送出的訊息，在 Discord 上被客服刪掉
    web = TicketMessage(
        ticket_id=ticket.id, source=Source.WEB, discord_message_id=200, author_id=5, content="網頁"
    )
    db.add(web)
    await db.flush()
    db.add(
        TicketAttachment(
            ticket_id=ticket.id,
            message_id=web.id,
            uploader_id=5,
            source=Source.WEB,
            filename="b.png",
            size=10,
            share_token_hash="0" * 64,
        )
    )
    await save_discord_message(db, ticket, make_message(300, []))

    await delete_discord_messages(db, [100, 200, 999])
    # 重複收到不會再記錄一次
    await delete_discord_messages(db, [100])

    rows = list(
        await db.scalars(
            select(TicketMessage)
            .order_by(TicketMessage.id)
            .execution_options(populate_existing=True)
        )
    )
    # 內容保留
    assert [(m.content, m.deleted_at is not None) for m in rows] == [
        ("你好", True),
        ("網頁", True),
        ("你好", False),
    ]
    revoked = {
        a.filename: a.share_revoked_at is not None
        for a in await db.scalars(
            select(TicketAttachment).execution_options(populate_existing=True)
        )
    }
    assert revoked == {"a.png": True, "b.png": True}
    # Discord 不會告訴 bot 是誰刪的
    assert await event_types(db) == [
        ("message.created", 5),
        ("message.created", 5),
        ("message.deleted", None),
        ("message.deleted", None),
    ]


async def test_delete_after_close_is_ignored(db: AsyncSession) -> None:
    ticket = await make_ticket(db)
    await save_discord_message(db, ticket, make_message(100, []))
    ticket.status = TicketStatus.CLOSED
    await db.commit()
    await delete_discord_messages(db, [100])
    saved = await db.scalar(select(TicketMessage))
    assert saved is not None and saved.deleted_at is None
