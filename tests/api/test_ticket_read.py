from datetime import UTC, datetime
from typing import Any

from sqlalchemy import update

from bot.core.config import get_settings
from bot.db.models import (
    AttachmentStatus,
    Source,
    TicketAttachment,
    TicketMessage,
    TicketMessageRevision,
    TicketUser,
)
from tests.api.helpers import Env
from tests.api.test_ticket_actions import ADMIN, OPENER, OTHER, STAFF, login_as, open_ticket
from tests.api.test_web_messages import upload


async def open_as(env: Env, user_id: int, category_id: int = 1) -> int:
    await login_as(env, user_id)
    response = await env.post(
        "/tickets", {"category_id": category_id, "subject": "主旨", "content": "內容"}
    )
    assert response.status_code == 201, response.json()
    ticket_id: int = response.json()["id"]
    return ticket_id


async def listed(env: Env, query: str = "") -> list[int]:
    response = await env.client.get(f"/tickets{query}")
    assert response.status_code == 200, response.json()
    return [item["id"] for item in response.json()]


async def test_list_visibility(env: Env) -> None:
    mine, _ = await open_ticket(env)
    others = await open_as(env, OTHER)

    await login_as(env, OPENER)
    response = await env.client.get("/tickets")
    [item] = response.json()
    assert item["id"] == mine
    assert item["opener"]["id"] == str(OPENER)
    assert item["opener"]["avatar_url"] == "https://cdn.discordapp.com/avatars/a.webp"
    assert item["claimed_by"] is None

    await login_as(env, OTHER)
    assert await listed(env) == [others]
    # 被加進客服單的成員也看得到
    await login_as(env, OPENER)
    assert (await env.post(f"/tickets/{mine}/members", {"user_id": str(OTHER)})).is_success
    await login_as(env, OTHER)
    assert await listed(env) == [others, mine]

    # 分類的客服、管理員看得到這個分類所有的單
    for user_id in (STAFF, ADMIN):
        await login_as(env, user_id)
        assert await listed(env) == [others, mine]


async def test_list_filters(env: Env) -> None:
    first, _ = await open_ticket(env)
    second, third = await open_as(env, OPENER), await open_as(env, OPENER)
    await login_as(env, STAFF)
    assert (await env.post(f"/tickets/{second}/claim", {})).is_success
    assert (await env.post(f"/tickets/{third}/close", {})).is_success

    assert await listed(env, "?status=closed") == [third]
    assert await listed(env, "?claimed=false") == [third, first]
    assert await listed(env, f"?claimed_by_id={STAFF}") == [second]
    assert await listed(env, f"?opener_id={OPENER}&category_id=1") == [third, second, first]
    claimed = (await env.client.get(f"/tickets?claimed_by_id={STAFF}")).json()[0]
    assert claimed["claimed_by"]["id"] == str(STAFF)

    # 分頁：新的在前面
    assert await listed(env, "?limit=2") == [third, second]
    assert await listed(env, f"?limit=2&before={second}") == [first]

    # 用開單者的名稱搜尋，不分大小寫；% 之類的字元照字面比對
    async with env.sessionmaker() as db:
        await db.execute(
            update(TicketUser)
            .where(TicketUser.ticket_id == first, TicketUser.user_id == OPENER)
            .values(username="xiao_jhuo")
        )
        await db.commit()
    assert await listed(env, "?q=JHUO") == [first]
    assert await listed(env, "?q=o_j") == [first]
    assert await listed(env, "?q=%25") == []


async def test_detail(env: Env) -> None:
    ticket_id, _ = await open_ticket(env)
    assert (await env.post(f"/tickets/{ticket_id}/members", {"user_id": str(OTHER)})).is_success
    async with env.sessionmaker() as db:
        await db.execute(
            update(TicketUser)
            .where(TicketUser.user_id == OPENER)
            .values(avatar_key=f"avatars/{OPENER}/avatarhash.webp")
        )
        await db.commit()

    body = (await env.client.get(f"/tickets/{ticket_id}")).json()
    assert (body["subject"], body["content"]) == ("主旨", "內容")
    users = {user["id"]: user for user in body["users"]}
    # 頭像存到 S3 之後用 presigned URL
    assert users[str(OPENER)]["avatar_url"].startswith(f"https://s3.test/avatars/{OPENER}/")
    assert users[str(OTHER)]["nickname"] == "暱稱"
    assert [(m["user_id"], m["added_by_id"]) for m in body["members"]] == [
        (str(OTHER), str(OPENER))
    ]

    assert (await env.post(f"/tickets/{ticket_id}/close", {"reason": "已解決"})).is_success
    assert (await env.put(f"/tickets/{ticket_id}/rating", {"rating": 4})).is_success
    assert (await env.client.get(f"/tickets/{ticket_id}")).json()["rating"] == 4
    await login_as(env, STAFF)
    body = (await env.client.get(f"/tickets/{ticket_id}")).json()
    assert (body["rating"], body["close_reason"]) == (4, "已解決")
    # 額外加入的成員看不到評分
    await login_as(env, OTHER)
    assert (await env.client.get(f"/tickets/{ticket_id}")).json()["rating"] is None

    await login_as(env, ADMIN)
    assert (await env.client.get("/tickets/999")).status_code == 404


async def messages(env: Env, ticket_id: int, query: str = "") -> Any:
    response = await env.client.get(f"/tickets/{ticket_id}/messages{query}")
    assert response.status_code == 200, response.json()
    return response.json()


async def test_messages(env: Env) -> None:
    ticket_id, _ = await open_ticket(env)
    attachment_id = await upload(env, ticket_id)
    url = f"/tickets/{ticket_id}/messages"
    ids = []
    for index in range(5):
        attachment_ids = [attachment_id] if index == 4 else []
        body = {"content": f"訊息 {index}", "attachment_ids": attachment_ids}
        ids.append((await env.post(url, body)).json()["id"])

    page = await messages(env, ticket_id, "?limit=2")
    # 舊的在前面
    assert [m["id"] for m in page["messages"]] == ids[3:]
    assert [u["id"] for u in page["users"]] == [str(OPENER)]
    [attachment] = page["messages"][1]["attachments"]
    # 還在檢查的附件沒有下載網址
    assert (attachment["id"], attachment["download_url"]) == (attachment_id, None)
    assert [m["id"] for m in (await messages(env, ticket_id, f"?before={ids[3]}"))["messages"]] == (
        ids[:3]
    )
    after = await messages(env, ticket_id, f"?after={ids[1]}&limit=2")
    assert [m["id"] for m in after["messages"]] == ids[2:4]

    async with env.sessionmaker() as db:
        await db.execute(
            update(TicketAttachment)
            .where(TicketAttachment.id == attachment_id)
            .values(status=AttachmentStatus.DONE, content_type="image/png")
        )
        await db.commit()
    page = await messages(env, ticket_id, f"?after={ids[3]}")
    download_url = page["messages"][0]["attachments"][0]["download_url"]
    assert download_url.startswith(f"https://s3.test/tickets/{ticket_id}/{attachment_id}/")

    await login_as(env, OTHER)
    assert (await env.client.get(url)).status_code == 404


async def test_edited_and_deleted_messages(env: Env) -> None:
    """被刪除的訊息、編輯前的內容、編輯時移除的附件，只有這張單的客服、管理員看得到。"""
    ticket_id, _ = await open_ticket(env)
    created_at = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
    edited_at = datetime(2026, 10, 24, 12, 5, tzinfo=UTC)
    async with env.sessionmaker() as db:
        edited = TicketMessage(
            ticket_id=ticket_id,
            source=Source.DISCORD,
            discord_message_id=100,
            author_id=OPENER,
            content="新的",
            created_at=created_at,
            edited_at=edited_at,
            stickers=[{"id": "300", "name": "揮手", "format": "apng"}],
        )
        deleted = TicketMessage(
            ticket_id=ticket_id,
            source=Source.DISCORD,
            discord_message_id=101,
            author_id=OPENER,
            content="貼錯了",
            created_at=created_at,
            deleted_at=edited_at,
            stickers=[{"id": "301", "name": "wumpus", "format": "lottie"}],
            # 轉發的訊息：被轉發的內容跟 content 一樣，刪除後只有客服看得到
            forwarded_content="被轉發的內容",
            forwarded_from={
                "created_at": "2026-10-01T08:00:00+00:00",
                "guild_id": "1",
                "channel_id": "2",
                "message_id": "3",
                "stickers": [{"id": "302", "name": "貓", "format": "gif"}],
            },
        )
        db.add_all([edited, deleted])
        await db.flush()
        db.add(
            TicketMessageRevision(
                message_id=edited.id, content="舊的", created_at=created_at, replaced_at=edited_at
            )
        )
        attachments = {
            filename: TicketAttachment(
                ticket_id=ticket_id,
                message_id=message.id,
                uploader_id=OPENER,
                source=Source.DISCORD,
                filename=filename,
                size=10,
                removed_at=removed_at,
            )
            for filename, message, removed_at in [
                ("kept.png", edited, None),
                ("removed.png", edited, edited_at),
                ("deleted.png", deleted, None),
            ]
        }
        db.add_all(attachments.values())
        await db.commit()

    def summary(page: Any) -> list[tuple[str, list[str], list[str], list[str]]]:
        return [
            (
                m["content"],
                [a["filename"] for a in m["attachments"]],
                [r["content"] for r in m["revisions"]],
                [s["url"] for s in m["stickers"]],
            )
            for m in page["messages"]
        ]

    stickers = f"{get_settings().public_base_url}/stickers"

    def status(filename: str) -> Any:
        return env.client.get(f"/attachments/{attachments[filename].id}")

    async def check_single(page: Any) -> None:
        """單則訊息的 API 跟列表的內容一樣。"""
        for message in page["messages"]:
            response = await env.client.get(f"/tickets/{ticket_id}/messages/{message['id']}")
            assert response.json() == {"messages": [message], "users": page["users"]}

    page = await messages(env, ticket_id)
    await check_single(page)
    # 開單者只看到已編輯、已刪除的標記
    assert summary(page) == [("新的", ["kept.png"], [], [f"{stickers}/300.png"]), ("", [], [], [])]
    assert page["messages"][0]["edited_at"] is not None
    assert page["messages"][1]["deleted_at"] is not None
    assert [m["forwarded"] for m in page["messages"]] == [None, None]
    assert (await status("kept.png")).status_code == 200
    assert (await status("removed.png")).status_code == 404
    assert (await status("deleted.png")).status_code == 404

    for user_id in (STAFF, ADMIN):
        await login_as(env, user_id)
        page = await messages(env, ticket_id)
        await check_single(page)
        assert summary(page) == [
            ("新的", ["kept.png", "removed.png"], ["舊的"], [f"{stickers}/300.png"]),
            ("貼錯了", ["deleted.png"], [], [f"{stickers}/301.json"]),
        ]
        assert page["messages"][1]["forwarded"] == {
            "content": "被轉發的內容",
            "created_at": "2026-10-01T08:00:00Z",
            "guild_id": "1",
            "channel_id": "2",
            "message_id": "3",
            "stickers": [
                {"id": "302", "name": "貓", "format": "gif", "url": f"{stickers}/302.gif"}
            ],
        }
        [revision] = page["messages"][0]["revisions"]
        assert revision["created_at"] < revision["replaced_at"]
        assert page["messages"][0]["attachments"][1]["removed_at"] is not None
        for filename in attachments:
            assert (await status(filename)).status_code == 200


async def test_get_message_not_found(env: Env) -> None:
    ticket_id, _ = await open_ticket(env)
    others = await open_as(env, OTHER)
    response = await env.post(f"/tickets/{others}/messages", {"content": "別張單"})
    message_id = response.json()["id"]

    await login_as(env, OPENER)
    # 別張單的訊息、不存在的訊息
    assert (await env.client.get(f"/tickets/{ticket_id}/messages/{message_id}")).status_code == 404
    assert (await env.client.get(f"/tickets/{ticket_id}/messages/999")).status_code == 404
    # 看不到的客服單
    assert (await env.client.get(f"/tickets/{others}/messages/{message_id}")).status_code == 404


async def test_get_attachment(env: Env) -> None:
    ticket_id, _ = await open_ticket(env)
    sent = await upload(env, ticket_id)
    assert (await env.post(f"/tickets/{ticket_id}/messages", {"attachment_ids": [sent]})).is_success
    unsent = await upload(env, ticket_id)
    async with env.sessionmaker() as db:
        await db.execute(update(TicketAttachment).values(status=AttachmentStatus.DONE))
        await db.commit()

    body = (await env.client.get(f"/attachments/{unsent}")).json()
    assert body["download_url"] is not None
    # 關單之後網頁一樣看得到（撤銷的只有分享連結）
    assert (await env.post(f"/tickets/{ticket_id}/close", {})).is_success
    await login_as(env, STAFF)
    assert (await env.client.get(f"/attachments/{sent}")).json()["download_url"] is not None
    # 還沒送出的附件只有上傳的人看得到
    assert (await env.client.get(f"/attachments/{unsent}")).status_code == 404
    await login_as(env, OTHER)
    assert (await env.client.get(f"/attachments/{sent}")).status_code == 404
    assert (await env.client.get("/attachments/999")).status_code == 404
