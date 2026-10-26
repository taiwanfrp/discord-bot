from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from discord.ext import commands
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from bot.cogs import guild_sync
from bot.db.models import GuildSettings, PermissionRole, RoleKind, TicketCategory
from tests.database import create_test_engine

# tests/conftest.py 設定的 GUILD_ID
GUILD = SimpleNamespace(id=1)


@pytest.fixture
async def cog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    engine = await create_test_engine(tmp_path)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as session:
        session.add(GuildSettings(id=1, log_channel_id=5, panel_channel_id=5, panel_message_id=9))
        session.add(TicketCategory(name="帳務", discord_category_id=5))
        session.add(PermissionRole(role_id=7, kind=RoleKind.ADMIN))
        await session.commit()
    monkeypatch.setattr(guild_sync, "get_sessionmaker", lambda: sessionmaker)
    yield guild_sync.GuildSync(cast(commands.Bot, None)), sessionmaker
    await engine.dispose()


async def test_channel_delete_clears_settings(cog: Any) -> None:
    sync, sessionmaker = cog
    await sync.on_guild_channel_delete(SimpleNamespace(id=5, guild=GUILD))
    async with sessionmaker() as session:
        settings = await session.get_one(GuildSettings, 1)
        assert settings.log_channel_id is None
        assert settings.panel_message_id is None
        category = await session.scalar(select(TicketCategory))
        assert category is not None and category.discord_category_id is None


async def test_role_delete_removes_permission(cog: Any) -> None:
    sync, sessionmaker = cog
    await sync.on_guild_role_delete(SimpleNamespace(id=7, guild=GUILD))
    async with sessionmaker() as session:
        assert await session.scalar(select(PermissionRole)) is None


async def test_other_guild_is_ignored(cog: Any) -> None:
    sync, sessionmaker = cog
    await sync.on_guild_role_delete(SimpleNamespace(id=7, guild=SimpleNamespace(id=2)))
    async with sessionmaker() as session:
        assert await session.scalar(select(PermissionRole)) is not None
