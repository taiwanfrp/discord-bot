from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

import pytest
from discord.ext import commands
from fakeredis import FakeAsyncRedis
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.api.app import create_app
from bot.api.deps import get_db
from bot.core.redis import get_redis, get_session_redis
from bot.db.base import Base
from bot.db.engine import create_engine
from bot.db.models import PermissionRole, RoleKind
from tests.api.helpers import ADMIN_ROLE_ID, Env, FakeBot


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as session:
        session.add(PermissionRole(role_id=ADMIN_ROLE_ID, kind=RoleKind.ADMIN))
        await session.commit()

    async def override_db() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    bot = FakeBot()
    redis = FakeAsyncRedis(decode_responses=True)
    sessions = FakeAsyncRedis(decode_responses=True)
    app = create_app(cast(commands.Bot, bot))
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_redis] = lambda: redis
    app.dependency_overrides[get_session_redis] = lambda: sessions

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://bot.test") as client:
        yield Env(client, bot, sessions, sessionmaker)
    await engine.dispose()
