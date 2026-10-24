from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import discord
import pytest
from discord.ext import commands
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.cogs import tickets as tickets_cog
from bot.db.base import Base
from bot.db.engine import create_engine
from bot.db.models import Source, Ticket, TicketCategory, TicketEvent, TicketStatus
from bot.tickets.permissions import MEMBER_PERMISSIONS, sync_role_overwrites
from tests.api.helpers import make_role, make_text_channel

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
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(tickets_cog, "get_sessionmaker", lambda: maker)
    yield maker
    await engine.dispose()


async def test_channel_delete_closes_ticket(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
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
