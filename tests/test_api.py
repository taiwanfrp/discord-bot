import hashlib
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from discord.ext import commands
from fakeredis import FakeAsyncRedis
from httpx import ASGITransport, AsyncClient
from redis.exceptions import ConnectionError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.api.app import create_app
from bot.api.deps import get_db
from bot.core.permissions import build_access
from bot.core.redis import get_redis, get_session_redis
from bot.db.base import Base
from bot.db.engine import create_engine
from bot.db.models import PermissionRole, RoleKind

ORIGIN = "https://web.test"
USER_ID = 2**60
ADMIN_ROLE_ID = 10


def make_user(user_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id,
        name="user",
        global_name="User",
        nick="暱稱",
        display_avatar=SimpleNamespace(url="https://cdn.discordapp.com/a.png"),
        roles=[SimpleNamespace(id=ADMIN_ROLE_ID)],
        guild_permissions=SimpleNamespace(administrator=False),
    )


class FakeGuild:
    def __init__(self) -> None:
        self.members: dict[int, SimpleNamespace] = {}
        self.channels: list[Any] = []
        self.roles = [
            SimpleNamespace(
                id=ADMIN_ROLE_ID,
                name="管理員",
                color=SimpleNamespace(value=0xFF0000),
                position=1,
                managed=False,
                is_assignable=lambda: True,
                is_default=lambda: False,
            )
        ]

    def get_member(self, user_id: int) -> SimpleNamespace | None:
        return self.members.get(user_id)


class FakeBot:
    def __init__(self) -> None:
        self.guild = FakeGuild()

    def get_guild(self, guild_id: int) -> FakeGuild:
        return self.guild

    async def fetch_user(self, user_id: int) -> SimpleNamespace:
        return make_user(user_id)

    def is_ready(self) -> bool:
        return True


class Env:
    def __init__(self, client: AsyncClient, bot: FakeBot, sessions: FakeAsyncRedis) -> None:
        self.client = client
        self.bot = bot
        self.sessions = sessions

    async def login_main_api(self, ttl: int = 100) -> str:
        """模擬主要 API：建立 session 與授權碼。"""
        session_hash = hashlib.sha256(b"main-session").hexdigest()
        await self.sessions.hset(f"session:{session_hash}", mapping={"discord_id": str(USER_ID)})
        await self.sessions.expire(f"session:{session_hash}", ttl)
        await self.sessions.set("ticket_code:code", session_hash, ex=30)
        return session_hash

    async def exchange(self) -> Any:
        return await self.client.post(
            "/auth/exchange", json={"code": "code"}, headers={"Origin": ORIGIN}
        )


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
        yield Env(client, bot, sessions)
    await engine.dispose()


async def test_exchange_sets_cookie(env: Env) -> None:
    await env.login_main_api(ttl=100)
    response = await env.exchange()

    assert response.status_code == 200
    assert response.json()["id"] == str(USER_ID)
    assert response.json()["in_guild"] is False
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("__Host-ticket_session=")
    for attribute in ("HttpOnly", "Max-Age=100", "Path=/", "SameSite=lax", "Secure"):
        assert attribute in cookie
    assert "Domain" not in cookie


async def test_code_can_only_be_used_once(env: Env) -> None:
    await env.login_main_api()
    assert (await env.exchange()).status_code == 200
    assert (await env.exchange()).status_code == 401


async def test_exchange_requires_origin_and_json(env: Env) -> None:
    await env.login_main_api()
    response = await env.client.post("/auth/exchange", json={"code": "code"})
    assert response.status_code == 403
    response = await env.client.post(
        "/auth/exchange", content="code=code", headers={"Origin": ORIGIN}
    )
    assert response.status_code == 415


async def test_me_follows_main_session(env: Env) -> None:
    assert (await env.client.get("/me")).status_code == 401

    session_hash = await env.login_main_api()
    await env.exchange()
    assert (await env.client.get("/me")).status_code == 200

    # 主要 API 登出（刪除 session）後立刻失效
    await env.sessions.delete(f"session:{session_hash}")
    assert (await env.client.get("/me")).status_code == 401


async def test_logout(env: Env) -> None:
    await env.login_main_api()
    await env.exchange()
    response = await env.client.post("/auth/logout", json={}, headers={"Origin": ORIGIN})
    assert response.status_code == 204
    assert (await env.client.get("/me")).status_code == 401


async def test_session_store_down_returns_503(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    await env.login_main_api()
    await env.exchange()

    async def broken(*args: object) -> None:
        raise ConnectionError

    monkeypatch.setattr(env.sessions, "hget", broken)
    assert (await env.client.get("/me")).status_code == 503


async def test_admin_only(env: Env) -> None:
    await env.login_main_api()
    await env.exchange()
    assert (await env.client.get("/guild/roles")).status_code == 403

    # 加入伺服器、擁有管理員身份組之後
    env.bot.guild.members[USER_ID] = make_user(USER_ID)
    response = await env.client.get("/guild/roles")
    assert response.status_code == 200
    assert response.json() == [
        {
            "id": str(ADMIN_ROLE_ID),
            "name": "管理員",
            "color": 0xFF0000,
            "position": 1,
            "managed": False,
            "assignable": True,
        }
    ]
    me = (await env.client.get("/me")).json()
    assert me["is_admin"] is True
    assert me["nickname"] == "暱稱"


async def test_cors_preflight(env: Env) -> None:
    headers = {
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type",
    }
    response = await env.client.options("/auth/exchange", headers={"Origin": ORIGIN, **headers})
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert response.headers["access-control-allow-credentials"] == "true"

    response = await env.client.options(
        "/auth/exchange", headers={"Origin": "https://evil.test", **headers}
    )
    assert "access-control-allow-origin" not in response.headers


def test_build_access() -> None:
    rows = [
        PermissionRole(role_id=1, kind=RoleKind.CATEGORY, category_id=5),
        PermissionRole(role_id=2, kind=RoleKind.CATEGORY, category_id=6),
    ]
    access = build_access(rows, is_guild_admin=False)
    assert not access.is_admin
    assert access.is_staff(5)
    assert not access.is_staff(7)

    assert build_access([], is_guild_admin=True).is_staff(7)
    assert build_access(
        [PermissionRole(role_id=1, kind=RoleKind.ADMIN)], is_guild_admin=False
    ).is_admin
