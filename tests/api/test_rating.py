import pytest
from sqlalchemy import text

from bot.db.models import Ticket
from bot.tickets import actions
from bot.tickets.errors import TicketError
from tests.api.helpers import Env
from tests.api.test_ticket_actions import OPENER, OTHER, STAFF, events, login_as, open_ticket


async def test_rating(env: Env) -> None:
    ticket_id, _ = await open_ticket(env)
    url = f"/tickets/{ticket_id}/rating"
    # 還沒關閉
    assert (await env.put(url, {"rating": 5})).status_code == 409
    assert (await env.post(f"/tickets/{ticket_id}/close", {})).is_success

    assert (await env.put(url, {"rating": 0})).status_code == 422
    assert (await env.put(url, {"rating": 6})).status_code == 422
    response = await env.put(url, {"rating": 5, "feedback": "  很快就解決了 "})
    assert response.status_code == 200, response.json()
    assert (response.json()["rating"], response.json()["feedback"]) == (5, "很快就解決了")
    assert (await events(env))[-1][0] == "ticket.rated"
    async with env.sessionmaker() as db:
        ticket = await db.get_one(Ticket, ticket_id)
        assert ticket.rated_at is not None
        raw = await db.scalar(text("SELECT feedback FROM ticket"))
        assert raw != "很快就解決了"

    # 只能一次
    assert (await env.put(url, {"rating": 1})).status_code == 409


async def test_only_opener_can_rate(env: Env) -> None:
    ticket_id, _ = await open_ticket(env)
    assert (await env.post(f"/tickets/{ticket_id}/close", {})).is_success
    url = f"/tickets/{ticket_id}/rating"
    await login_as(env, STAFF)
    assert (await env.put(url, {"rating": 5})).status_code == 403
    await login_as(env, OTHER)
    assert (await env.put(url, {"rating": 5})).status_code == 404


async def test_rate_at_the_same_time(env: Env) -> None:
    """網頁與 Discord 同時評分：兩邊讀到的都是還沒評分，只有一個會成功。"""
    ticket_id, _ = await open_ticket(env)
    assert (await env.post(f"/tickets/{ticket_id}/close", {})).is_success
    async with env.sessionmaker() as first, env.sessionmaker() as second:
        stale = await second.get_one(Ticket, ticket_id)
        await actions.rate(first, await first.get_one(Ticket, ticket_id), OPENER, 5, None)
        with pytest.raises(TicketError, match="已經評分過了"):
            await actions.rate(second, stale, OPENER, 1, None)
    async with env.sessionmaker() as db:
        assert (await db.get_one(Ticket, ticket_id)).rating == 5
