import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord.ext import commands
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.cogs import tickets as tickets_cog
from bot.db.models import (
    Source,
    Ticket,
    TicketCategory,
    TicketEvent,
    TicketMessage,
    TicketMessageRevision,
    TicketStatus,
)
from bot.tickets.permissions import MEMBER_PERMISSIONS, sync_role_overwrites
from tests.api.helpers import make_member, make_role, make_text_channel
from tests.database import create_test_engine

GUILD_ID = 1


async def test_sync_role_overwrites() -> None:
    roles = {role_id: make_role(role_id) for role_id in (10, 20, 99)}
    guild = SimpleNamespace(id=GUILD_ID, get_role=roles.get)
    channel = make_text_channel(900)
    channel.overwrites = {
        discord.Object(GUILD_ID, type=discord.Role): discord.PermissionOverwrite(
            view_channel=False
        ),
        roles[10]: MEMBER_PERMISSIONS,
        # 不再是權限身份組（REST 取得的頻道，對應不到快取時是 Object）
        discord.Object(99, type=discord.Role): MEMBER_PERMISSIONS,
        discord.Object(5, type=discord.User): MEMBER_PERMISSIONS,
    }
    ticket = Ticket(id=1)

    await sync_role_overwrites(channel, cast(discord.Guild, guild), {10, 20}, ticket)

    calls = {
        call.args[0].id: call.kwargs["overwrite"] for call in channel.set_permissions.call_args_list
    }
    # 移除 99、加上 20；@everyone、成員、已經正確的 10 都不動
    assert calls == {99: None, 20: MEMBER_PERMISSIONS}


@pytest.fixture
async def sessionmaker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = await create_test_engine(tmp_path)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(tickets_cog, "get_sessionmaker", lambda: maker)
    yield maker
    await engine.dispose()


async def test_channel_delete_closes_ticket(
    sessionmaker: async_sessionmaker[AsyncSession], close_notifications: AsyncMock
) -> None:
    async with sessionmaker() as db:
        category = TicketCategory(name="帳務")
        db.add(category)
        await db.flush()
        db.add(
            Ticket(
                category_id=category.id,
                source=Source.WEB,
                opener_id=5,
                channel_id=900,
                subject="",
                content="",
            )
        )
        await db.commit()

    guild = MagicMock(id=GUILD_ID)
    guild.get_channel.return_value = None
    channel = cast(discord.abc.GuildChannel, SimpleNamespace(id=900, guild=guild))
    cog = tickets_cog.Tickets(cast(commands.Bot, None))
    await cog.on_guild_channel_delete(channel)
    # 再刪一次（例如 bot 關單時自己刪除頻道）不會重複關閉
    await cog.on_guild_channel_delete(channel)

    async with sessionmaker() as db:
        ticket = await db.get_one(Ticket, 1)
        assert ticket.status is TicketStatus.CLOSED
        assert ticket.closed_by_id is None
        events = list(await db.scalars(select(TicketEvent)))
        assert [event.type for event in events] == ["ticket.closed"]
    # 頻道被直接刪除也算關單，一樣私訊
    close_notifications.assert_awaited_once_with(1)


async def test_message_events_in_order(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """傳出後馬上被編輯、刪除：discord.py 每個事件各自一個 task，要依照收到的順序處理。"""
    async with sessionmaker() as db:
        category = TicketCategory(name="帳務")
        db.add(category)
        await db.flush()
        db.add(
            Ticket(
                category_id=category.id,
                source=Source.WEB,
                opener_id=5,
                channel_id=900,
                subject="",
                content="",
            )
        )
        await db.commit()

    created_at = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
    message = SimpleNamespace(
        id=100,
        guild=SimpleNamespace(id=GUILD_ID),
        channel=SimpleNamespace(id=900),
        author=make_member(5, []),
        type=discord.MessageType.default,
        webhook_id=None,
        content="你好",
        created_at=created_at,
        edited_at=None,
        attachments=[],
        stickers=[],
        message_snapshots=[],
        reference=None,
    )
    edited = SimpleNamespace(**{**vars(message), "content": "改過", "edited_at": created_at})
    cog = tickets_cog.Tickets(cast(commands.Bot, SimpleNamespace(user=SimpleNamespace(id=999))))
    await asyncio.gather(
        cog.on_message(cast(discord.Message, message)),
        cog.on_raw_message_edit(
            cast(discord.RawMessageUpdateEvent, SimpleNamespace(guild_id=GUILD_ID, message=edited))
        ),
        cog.on_raw_message_delete(
            cast(discord.RawMessageDeleteEvent, SimpleNamespace(guild_id=GUILD_ID, message_id=100))
        ),
    )

    async with sessionmaker() as db:
        saved = await db.scalar(select(TicketMessage))
        assert saved is not None
        assert (saved.content, saved.deleted_at is not None) == ("改過", True)
        assert await db.scalar(select(TicketMessageRevision.content)) == "你好"
        events = await db.scalars(select(TicketEvent.type).order_by(TicketEvent.id))
        assert list(events) == ["message.created", "message.updated", "message.deleted"]
