from typing import Any
from unittest.mock import AsyncMock

import discord
from sqlalchemy import select, text

from bot.db.models import Ticket, TicketEvent, TicketStatus, TicketUser
from bot.tickets import open as open_module
from tests.api.helpers import USER_ID, Env, make_category_channel, make_role


async def setup_category(env: Env, **category: Any) -> int:
    """建立 Discord 類別、管理員以外的分類身份組與一個分類，回傳分類 id。"""
    env.guild.channels.append(make_category_channel(600))
    env.guild.roles.append(make_role(20))
    await env.login(admin=True)
    await env.patch("/settings", {"default_discord_category_id": "600"})
    created = (await env.post("/categories", {"name": "帳務", **category})).json()
    await env.put(
        "/permission-roles",
        {
            "admin_role_ids": ["10"],
            "category_roles": [{"category_id": created["id"], "role_ids": ["20"]}],
        },
    )
    return int(created["id"])


async def open_ticket(env: Env, category_id: int) -> Any:
    return await env.post(
        "/tickets", {"category_id": category_id, "subject": "無法連線", "content": "錯誤訊息…"}
    )


async def test_open_as_member(env: Env) -> None:
    category_id = await setup_category(env)
    response = await open_ticket(env, category_id)
    assert response.status_code == 201, response.json()
    body = response.json()
    assert body["status"] == "open"
    assert body["channel_id"] == "9000"
    assert body["opener_id"] == str(USER_ID)

    channel = env.guild.created_channels[0]
    name = env.guild.create_text_channel.call_args.args[0]
    kwargs = env.guild.create_text_channel.call_args.kwargs
    assert name == f"客服單-{body['id']}"
    assert kwargs["category"].id == 600
    overwrites = {target.id: overwrite for target, overwrite in kwargs["overwrites"].items()}
    assert overwrites[0].view_channel is False  # @everyone
    assert overwrites[USER_ID].send_messages is True  # 開單者
    assert overwrites[10].view_channel is True  # 管理員身份組
    assert overwrites[20].view_channel is True  # 分類身份組

    sent = channel.send.call_args.kwargs
    assert sent["content"] == f"<@{USER_ID}>"
    assert sent["embed"].title == f"#{body['id']} 無法連線"
    assert [field.name for field in sent["embed"].fields] == ["分類", "開單者", "來源", "接手"]
    assert sent["embed"].fields[-1].value == "尚未接手"
    # 還沒有人接手：只有「接手」，沒有「釋放」
    assert [item.custom_id for item in sent["view"].children] == [
        "ticket:close",
        "ticket:close_with_reason",
        "ticket:claim",
    ]

    async with env.sessionmaker() as db:
        ticket = await db.get_one(Ticket, body["id"])
        assert ticket.webhook_id == 7
        assert ticket.webhook_token == "webhook-token"
        assert ticket.opener_added_at is not None
        assert ticket.opening_message_id == channel.id * 10
        raw = (await db.execute(text("SELECT webhook_token FROM ticket"))).scalar_one()
        assert raw != "webhook-token"
        snapshot = await db.get_one(TicketUser, (ticket.id, USER_ID))
        assert (snapshot.nickname, snapshot.avatar_hash, snapshot.role_name) == (
            "暱稱",
            "avatarhash",
            "管理員",
        )
        event = await db.scalar(select(TicketEvent))
        assert event is not None and event.type == "ticket.created"


async def test_long_content_is_split(env: Env) -> None:
    """embed 只放內容的開頭，剩下的接在後面送出（Discord 對訊息的大小有上限）。"""
    category_id = await setup_category(env)
    content = ("中" * 900 + "\n") * 4 + "@everyone"
    response = await env.post(
        "/tickets", {"category_id": category_id, "subject": "主" * 100, "content": content}
    )
    assert response.status_code == 201, response.json()

    channel = env.guild.created_channels[0]
    first, *rest = channel.send.call_args_list
    description = first.kwargs["embed"].description
    assert len(description) <= open_module.EMBED_CONTENT_LIMIT
    assert description == "中" * 900 + open_module.CONTINUED
    chunks = [call.args[0] for call in rest]
    assert all(len(chunk) <= open_module.MESSAGE_LIMIT for chunk in chunks)
    line = "中" * 900
    assert chunks == [f"{line}\n{line}", f"{line}\n@everyone"]
    assert all(call.kwargs["allowed_mentions"].everyone is False for call in rest)
    async with env.sessionmaker() as db:
        # 資料庫裡是完整的內容
        assert (await db.get_one(Ticket, response.json()["id"])).content == content


def test_split_content_without_newlines() -> None:
    head, rest = open_module.split_content("a" * 5000)
    assert head.endswith(open_module.CONTINUED)
    assert "".join([head.removesuffix(open_module.CONTINUED), *rest]) == "a" * 5000
    first = len(head) - len(open_module.CONTINUED)
    assert [len(chunk) for chunk in rest] == [2000, 2000, 5000 - 4000 - first]
    assert open_module.split_content("短的") == ("短的", [])


async def test_open_when_not_in_guild(env: Env) -> None:
    category_id = await setup_category(env)
    await env.login()
    env.guild.fetch_ban = AsyncMock()  # 已被停權
    response = await open_ticket(env, category_id)
    assert response.status_code == 201

    kwargs = env.guild.create_text_channel.call_args.kwargs
    assert USER_ID not in {target.id for target in kwargs["overwrites"]}
    sent = env.guild.created_channels[0].send.call_args.kwargs
    assert sent["content"] is None
    note = sent["embed"].fields[-1]
    assert note.name == "注意" and "已被停權" in note.value
    async with env.sessionmaker() as db:
        ticket = await db.get_one(Ticket, response.json()["id"])
        assert ticket.opener_added_at is None


async def test_open_ticket_limit(env: Env) -> None:
    category_id = await setup_category(env)
    await env.patch("/settings", {"max_open_tickets_per_user": 2})
    assert (await open_ticket(env, category_id)).status_code == 201
    assert (await open_ticket(env, category_id)).status_code == 201
    response = await open_ticket(env, category_id)
    assert response.status_code == 409
    assert "最多只能開 2 張" in response.json()["detail"]


async def test_category_checks(env: Env) -> None:
    category_id = await setup_category(env, enabled=False)
    assert (await open_ticket(env, category_id)).status_code == 409
    assert (await open_ticket(env, 999)).status_code == 404

    # 分類和全域都沒有設定 Discord 類別
    await env.patch(f"/categories/{category_id}", {"enabled": True})
    await env.patch("/settings", {"default_discord_category_id": None})
    response = await open_ticket(env, category_id)
    assert response.status_code == 409
    assert "Discord 類別" in response.json()["detail"]


async def test_failure_cleans_up(env: Env) -> None:
    category_id = await setup_category(env)
    await env.patch("/settings", {"max_open_tickets_per_user": 1})

    async def broken_channel(name: str, **_: Any) -> Any:
        channel = await env.guild._create_text_channel(name)
        channel.create_webhook.side_effect = discord.HTTPException(AsyncMock(status=500), "boom")
        return channel

    env.guild.create_text_channel.side_effect = broken_channel
    response = await open_ticket(env, category_id)
    assert response.status_code == 502
    env.guild.created_channels[0].delete.assert_awaited_once()
    async with env.sessionmaker() as db:
        ticket = await db.scalar(select(Ticket))
        assert ticket is not None and ticket.status is TicketStatus.FAILED

    # 失敗的客服單不算在開單上限裡
    env.guild.create_text_channel.side_effect = env.guild._create_text_channel
    assert (await open_ticket(env, category_id)).status_code == 201
