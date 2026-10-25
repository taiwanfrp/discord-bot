import pytest
from redis.exceptions import ConnectionError

from bot.core.permissions import build_access
from bot.db.models import PermissionRole, RoleKind
from tests.api.helpers import ORIGIN, USER_ID, Env


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
    await env.login()
    response = await env.post("/auth/logout", {})
    assert response.status_code == 204
    assert (await env.client.get("/me")).status_code == 401


async def test_session_store_down_returns_503(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    await env.login()

    async def broken(*args: object) -> None:
        raise ConnectionError

    monkeypatch.setattr(env.sessions, "hget", broken)
    assert (await env.client.get("/me")).status_code == 503


async def test_admin_only(env: Env) -> None:
    await env.login()
    assert (await env.client.get("/guild/roles")).status_code == 403

    await env.login(admin=True)
    response = await env.client.get("/guild/roles")
    assert response.status_code == 200
    assert response.json()[0]["id"] == str(env.guild.roles[0].id)
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


async def test_event_streams_require_login_and_access(env: Env) -> None:
    assert (await env.client.get("/events")).status_code == 401
    await env.login()
    assert (await env.client.get("/tickets/999/events")).status_code == 404
