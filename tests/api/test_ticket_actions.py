from typing import Any
from unittest.mock import AsyncMock

from sqlalchemy import select, text

from bot.db.models import AttachmentStatus, Source, Ticket, TicketAttachment, TicketEvent
from bot.tickets.log import ticket_url
from tests.api.helpers import (
    ADMIN_ROLE_ID,
    USER_ID,
    Env,
    make_category_channel,
    make_role,
    make_text_channel,
)

OPENER = USER_ID
STAFF, STAFF2, ADMIN, OTHER = USER_ID + 1, USER_ID + 2, USER_ID + 3, USER_ID + 4
STAFF_ROLE = 20


async def open_ticket(env: Env, *, opener_in_guild: bool = True) -> tuple[int, Any]:
    """管理員設定好分類後，由開單者開一張單。回傳客服單 id 與頻道（mock）。"""
    env.guild.channels.append(make_category_channel(600))
    env.guild.roles.append(make_role(STAFF_ROLE))
    await env.login(admin=True, user_id=ADMIN)
    await env.patch("/settings", {"default_discord_category_id": "600"})
    category = (await env.post("/categories", {"name": "帳務"})).json()
    await env.put(
        "/permission-roles",
        {
            "admin_role_ids": [str(ADMIN_ROLE_ID)],
            "category_roles": [{"category_id": category["id"], "role_ids": [str(STAFF_ROLE)]}],
        },
    )
    for user_id in (STAFF, STAFF2):
        env.add_member(user_id, [STAFF_ROLE])
    env.add_member(OTHER, [])

    await env.login(role_ids=[] if opener_in_guild else None, user_id=OPENER)
    response = await env.post(
        "/tickets", {"category_id": category["id"], "subject": "主旨", "content": "內容"}
    )
    assert response.status_code == 201, response.json()
    return response.json()["id"], env.guild.created_channels[-1]


async def login_as(env: Env, user_id: int) -> None:
    roles = {STAFF: [STAFF_ROLE], STAFF2: [STAFF_ROLE], ADMIN: [ADMIN_ROLE_ID]}.get(user_id, [])
    await env.login(role_ids=roles, user_id=user_id)


async def events(env: Env) -> list[tuple[str, int | None, Any]]:
    async with env.sessionmaker() as db:
        rows = await db.scalars(select(TicketEvent).order_by(TicketEvent.id))
        return [(row.type, row.actor_id, row.data) for row in rows]


def last_sent(channel: Any) -> str:
    return str(channel.send.call_args.args[0])


async def test_claim_and_unclaim(env: Env) -> None:
    ticket_id, channel = await open_ticket(env)
    url = f"/tickets/{ticket_id}"

    # 開單者不能接手
    assert (await env.post(f"{url}/claim", {})).status_code == 403

    await login_as(env, STAFF)
    response = await env.post(f"{url}/claim", {})
    assert response.status_code == 200
    assert response.json()["claimed_by_id"] == str(STAFF)
    assert "接手了" in last_sent(channel)
    edited = channel.opening.edit.call_args.kwargs
    assert edited["embeds"][0].fields[0].value == f"<@{STAFF}>"
    assert [item.custom_id for item in edited["view"].children] == [
        "ticket:close",
        "ticket:close_with_reason",
        "ticket:unclaim",
    ]

    # 已經有人接手：其他客服不能搶，也不能釋放
    await login_as(env, STAFF2)
    assert (await env.post(f"{url}/claim", {})).status_code == 409
    assert (await env.post(f"{url}/unclaim", {})).status_code == 403

    # 管理員可以釋放別人接手的單
    await login_as(env, ADMIN)
    response = await env.post(f"{url}/unclaim", {})
    assert response.status_code == 200
    assert response.json()["claimed_by_id"] is None
    assert "ticket:claim" in [
        item.custom_id for item in channel.opening.edit.call_args.kwargs["view"].children
    ]
    assert [e[:2] for e in await events(env)][1:] == [
        ("ticket.claimed", STAFF),
        ("ticket.unclaimed", ADMIN),
    ]


async def test_transfer(env: Env) -> None:
    ticket_id, _ = await open_ticket(env)
    url = f"/tickets/{ticket_id}"
    await login_as(env, STAFF)
    await env.post(f"{url}/claim", {})

    # 只能轉給這個分類的客服或管理員
    assert (await env.post(f"{url}/transfer", {"user_id": str(OTHER)})).status_code == 422
    assert (await env.post(f"{url}/transfer", {"user_id": "999"})).status_code == 422
    response = await env.post(f"{url}/transfer", {"user_id": str(STAFF2)})
    assert response.status_code == 200
    assert response.json()["claimed_by_id"] == str(STAFF2)

    # 已經不是接手的人，不能再轉交
    assert (await env.post(f"{url}/transfer", {"user_id": str(STAFF)})).status_code == 403
    await login_as(env, ADMIN)
    assert (await env.post(f"{url}/transfer", {"user_id": str(STAFF)})).status_code == 200
    transferred = [e for e in await events(env) if e[0] == "ticket.transferred"]
    assert transferred == [
        ("ticket.transferred", STAFF, {"from_id": str(STAFF), "to_id": str(STAFF2)}),
        ("ticket.transferred", ADMIN, {"from_id": str(STAFF2), "to_id": str(STAFF)}),
    ]


async def test_members(env: Env) -> None:
    ticket_id, channel = await open_ticket(env)
    url = f"/tickets/{ticket_id}/members"

    assert (await env.post(url, {"user_id": str(OTHER)})).status_code == 204
    (target,) = channel.set_permissions.call_args.args
    assert target.id == OTHER
    assert channel.set_permissions.call_args.kwargs["overwrite"].view_channel is True
    assert (await env.post(url, {"user_id": str(OTHER)})).status_code == 409
    # 開單者與客服本來就看得到
    assert (await env.post(url, {"user_id": str(OPENER)})).status_code == 422
    assert (await env.post(url, {"user_id": str(STAFF)})).status_code == 422

    # 額外加入的成員看得到客服單，但不能關閉或管理成員
    await login_as(env, OTHER)
    assert (await env.post(f"/tickets/{ticket_id}/close", {})).status_code == 403
    assert (await env.delete(f"{url}/{OTHER}")).status_code == 403

    # 已經離開伺服器的人也可以移除
    await login_as(env, OPENER)
    env.guild.members.pop(OTHER)
    assert (await env.delete(f"{url}/{OTHER}")).status_code == 204
    assert channel.set_permissions.call_args.kwargs["overwrite"] is None
    assert (await env.delete(f"{url}/{OTHER}")).status_code == 404
    assert [e[0] for e in await events(env)][1:] == ["ticket.member_added", "ticket.member_removed"]


async def test_close(env: Env, close_notifications: AsyncMock) -> None:
    ticket_id, channel = await open_ticket(env)
    log_channel = make_text_channel(500)
    env.guild.channels.append(log_channel)
    await login_as(env, ADMIN)
    await env.patch("/settings", {"log_channel_id": "500"})
    async with env.sessionmaker() as db:
        db.add(
            TicketAttachment(
                ticket_id=ticket_id,
                uploader_id=OPENER,
                source=Source.WEB,
                filename="a.png",
                size=1,
                status=AttachmentStatus.DONE,
                share_token_hash="0" * 64,
            )
        )
        await db.commit()

    await login_as(env, OPENER)
    response = await env.post(f"/tickets/{ticket_id}/close", {"reason": "已解決"})
    assert response.status_code == 200
    assert response.json()["status"] == "closed"
    assert response.json()["closed_by_id"] == str(OPENER)
    channel.delete.assert_awaited_once()
    assert log_channel.send.call_args.kwargs["embed"].title == f"客服單 #{ticket_id} 已關閉"
    [link] = log_channel.send.call_args.kwargs["view"].children
    assert link.url == ticket_url(ticket_id)
    # 私訊由 worker 送出
    close_notifications.assert_awaited_once_with(ticket_id)

    async with env.sessionmaker() as db:
        ticket = await db.get_one(Ticket, ticket_id)
        assert ticket.close_reason == "已解決"
        raw = (await db.execute(text("SELECT close_reason FROM ticket"))).scalar_one()
        assert raw != "已解決"
        attachment = await db.scalar(select(TicketAttachment))
        assert attachment is not None and attachment.share_revoked_at is not None

    # 已經關閉：不能再關閉或操作
    assert (await env.post(f"/tickets/{ticket_id}/close", {})).status_code == 409
    await login_as(env, STAFF)
    assert (await env.post(f"/tickets/{ticket_id}/claim", {})).status_code == 409


async def test_opener_not_in_guild_can_close_from_web(env: Env) -> None:
    ticket_id, _ = await open_ticket(env, opener_in_guild=False)
    response = await env.post(f"/tickets/{ticket_id}/close", {})
    assert response.status_code == 200
    # 不在伺服器裡，不能做需要身份組的事
    ticket_id, _ = await open_ticket(env, opener_in_guild=False)
    assert (await env.post(f"/tickets/{ticket_id}/claim", {})).status_code == 403


async def test_other_users_cannot_see_ticket(env: Env) -> None:
    ticket_id, _ = await open_ticket(env)
    await login_as(env, OTHER)
    assert (await env.post(f"/tickets/{ticket_id}/close", {})).status_code == 404
    assert (await env.post("/tickets/999/close", {})).status_code == 404


async def test_permission_change_queues_sync(env: Env) -> None:
    await open_ticket(env)
    # open_ticket 裡設定權限身份組時放入了一次
    env.sync_permissions.assert_awaited_once()
