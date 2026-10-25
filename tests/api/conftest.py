from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from discord.ext import commands
from fakeredis import FakeAsyncRedis
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.api.app import create_app
from bot.api.deps import get_db, get_db_sessionmaker
from bot.api.sse import EventHub
from bot.core.redis import get_redis, get_session_redis
from bot.db.base import Base
from bot.db.engine import create_engine
from bot.db.models import PermissionRole, RoleKind
from bot.worker import tasks
from tests.api.helpers import ADMIN_ROLE_ID, Env, FakeBot
from tests.fake_s3 import FakeS3


@pytest.fixture
async def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Env]:
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
    s3 = FakeS3()
    app = create_app(cast(commands.Bot, bot), EventHub(lambda: sessionmaker), cast(Any, s3))
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_db_sessionmaker] = lambda: sessionmaker
    app.dependency_overrides[get_redis] = lambda: redis
    app.dependency_overrides[get_session_redis] = lambda: sessions

    # 不真的放進 queue；測試可以檢查有沒有呼叫
    sync_permissions = AsyncMock()
    monkeypatch.setattr(tasks.sync_ticket_permissions, "kiq", sync_permissions)
    verify = AsyncMock()
    monkeypatch.setattr(tasks.verify_attachment, "kiq", verify)
    deliver = AsyncMock()
    monkeypatch.setattr(tasks.deliver_messages, "kiq", deliver)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://bot.test") as client:
        env = Env(client, bot, sessions, sessionmaker)
        env.sync_permissions = sync_permissions
        env.verify = verify
        env.deliver = deliver
        env.s3 = s3
        env.redis = redis
        yield env
    await engine.dispose()
