from sqlalchemy import select, text

from bot.db.models import Ticket, TicketEvent, TicketMessage, TicketUser
from tests.api.helpers import Env
from tests.api.test_attachments import create
from tests.api.test_ticket_actions import OPENER, OTHER, STAFF, login_as, open_ticket
from tests.test_files import PNG


async def upload(env: Env, ticket_id: int, filename: str = "a.png") -> int:
    body = (await create(env, ticket_id, filename, len(PNG))).json()
    async with env.sessionmaker() as db:
        key = await db.scalar(
            text("SELECT storage_key FROM ticket_attachment WHERE id = :id"),
            {"id": body["attachment"]["id"]},
        )
    env.s3.objects[key] = PNG
    assert (await env.post(f"/attachments/{body['attachment']['id']}/complete", {})).is_success
    attachment_id: int = body["attachment"]["id"]
    return attachment_id


async def test_send_message(env: Env) -> None:
    ticket_id, _ = await open_ticket(env)
    attachment_id = await upload(env, ticket_id)
    url = f"/tickets/{ticket_id}/messages"
    response = await env.post(url, {"content": "網頁的訊息", "attachment_ids": [attachment_id]})
    assert response.status_code == 201, response.json()
    body = response.json()
    assert body["delivery_status"] == "pending"
    assert body["author_id"] == str(OPENER)
    assert [a["id"] for a in body["attachments"]] == [attachment_id]
    assert body["attachments"][0]["message_id"] == body["id"]
    env.deliver.assert_awaited_once_with(ticket_id)

    async with env.sessionmaker() as db:
        message = await db.get_one(TicketMessage, body["id"])
        assert message.content == "網頁的訊息"
        raw = await db.scalar(text("SELECT content FROM ticket_message"))
        assert raw != "網頁的訊息"
        ticket = await db.get_one(Ticket, ticket_id)
        assert ticket.last_message_at == message.created_at
        assert await db.get(TicketUser, (ticket_id, OPENER)) is not None
        event = await db.scalar(select(TicketEvent).order_by(TicketEvent.id.desc()))
        assert event is not None
        assert (event.type, event.data) == ("message.created", {"message_id": str(body["id"])})

    # 同一個附件不能送兩次
    response = await env.post(url, {"content": "x", "attachment_ids": [attachment_id]})
    assert response.status_code == 422


async def test_invalid_messages(env: Env) -> None:
    ticket_id, _ = await open_ticket(env)
    url = f"/tickets/{ticket_id}/messages"
    assert (await env.post(url, {"content": "  "})).status_code == 422
    assert (await env.post(url, {"content": "x" * 2001})).status_code == 422
    # 還沒上傳完成的附件
    pending = (await create(env, ticket_id)).json()["attachment"]["id"]
    assert (await env.post(url, {"attachment_ids": [pending]})).status_code == 422
    # 加上附件連結之後超過 Discord 的上限
    attachment_id = await upload(env, ticket_id)
    response = await env.post(url, {"content": "x" * 1950, "attachment_ids": [attachment_id]})
    assert response.status_code == 422
    response = await env.post(url, {"content": "x" * 1900, "attachment_ids": [attachment_id]})
    assert response.status_code == 201
    env.deliver.assert_awaited_once()

    # 別人的附件
    other = await upload(env, ticket_id)
    await login_as(env, STAFF)
    assert (await env.post(url, {"attachment_ids": [other]})).status_code == 422
    # 看不到的客服單
    await login_as(env, OTHER)
    assert (await env.post(url, {"content": "x"})).status_code == 404


async def test_closed_ticket(env: Env) -> None:
    ticket_id, _ = await open_ticket(env)
    assert (await env.post(f"/tickets/{ticket_id}/close", {})).status_code == 200
    response = await env.post(f"/tickets/{ticket_id}/messages", {"content": "x"})
    assert response.status_code == 409
