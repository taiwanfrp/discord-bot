from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from taskiq.exceptions import SendTaskError

from bot.api.routes import exports
from bot.db.models import ExportStatus, TicketExport
from bot.worker import tasks
from tests.api.helpers import Env
from tests.api.test_ticket_actions import ADMIN, OPENER, OTHER, STAFF, login_as, open_ticket


@pytest.fixture
def queue(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    kiq = AsyncMock()
    monkeypatch.setattr(tasks.export_ticket, "kiq", kiq)
    return kiq


async def test_create_and_download(env: Env, queue: AsyncMock) -> None:
    ticket_id, _ = await open_ticket(env)
    response = await env.post(f"/tickets/{ticket_id}/exports", {"utc_offset": 480})
    assert response.status_code == 201
    created = response.json()
    assert created["status"] == "pending" and created["download_url"] is None
    queue.assert_awaited_once_with(created["id"])
    # 還沒完成時再按一次：回傳同一個
    again = await env.post(f"/tickets/{ticket_id}/exports", {"utc_offset": 480})
    assert again.json()["id"] == created["id"]
    assert queue.await_count == 1

    async with env.sessionmaker() as db:
        row = await db.get_one(TicketExport, created["id"])
        assert (row.requested_by_id, row.staff, row.utc_offset) == (OPENER, False, 480)
        row.status = ExportStatus.DONE
        row.format = "zip"
        row.storage_key = f"exports/{row.id}/ticket-{ticket_id}.zip"
        await db.commit()

    out = (await env.client.get(f"/exports/{created['id']}")).json()
    assert out["status"] == "done"
    assert out["download_url"].startswith(f"https://s3.test/exports/{created['id']}/")
    assert "ResponseContentType=application%2Fzip" in out["download_url"]
    assert f"ticket-{ticket_id}.zip" in out["download_url"]

    # 只有匯出的人自己拿得到
    await login_as(env, STAFF)
    assert (await env.client.get(f"/exports/{created['id']}")).status_code == 404


async def test_who_can_export(env: Env, queue: AsyncMock) -> None:
    ticket_id, _ = await open_ticket(env)
    await env.post(f"/tickets/{ticket_id}/members", {"user_id": str(OTHER)})
    url = f"/tickets/{ticket_id}/exports"

    for user_id in (STAFF, ADMIN):
        await login_as(env, user_id)
        response = await env.post(url, {"utc_offset": 0})
        assert response.status_code == 201
        async with env.sessionmaker() as db:
            assert (await db.get_one(TicketExport, response.json()["id"])).staff is True

    # 額外加入的成員看得到客服單，但不能匯出
    await login_as(env, OTHER)
    assert (await env.post(url, {"utc_offset": 0})).status_code == 403
    assert (await env.post(url, {"utc_offset": 15 * 60})).status_code == 422


async def test_queue_failure(env: Env, queue: AsyncMock) -> None:
    ticket_id, _ = await open_ticket(env)
    queue.side_effect = SendTaskError()
    response = await env.post(f"/tickets/{ticket_id}/exports", {"utc_offset": 0})
    assert response.json()["status"] == "failed"


async def test_rate_limit(env: Env, queue: AsyncMock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(exports, "RATE_LIMIT", 2)
    ticket_id, _ = await open_ticket(env)
    url = f"/tickets/{ticket_id}/exports"
    for _ in range(2):
        response = await env.post(url, {"utc_offset": 0})
        assert response.status_code == 201
        # 完成之後才能再匯出（還沒完成時會回傳同一個）
        async with env.sessionmaker() as db:
            row = await db.get_one(TicketExport, response.json()["id"])
            row.status = ExportStatus.DONE
            await db.commit()
    response = await env.post(url, {"utc_offset": 0})
    assert response.status_code == 429

    # 超過一小時的不算
    async with env.sessionmaker() as db:
        for row in await db.scalars(select(TicketExport)):
            row.created_at = datetime.now(UTC) - timedelta(hours=2)
        await db.commit()
    assert (await env.post(url, {"utc_offset": 0})).status_code == 201
