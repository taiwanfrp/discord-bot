from typing import Any
from urllib.parse import parse_qs, urlparse

from bot.db.models import TicketAttachment
from bot.storage.s3 import PART_SIZE
from tests.api.helpers import Env
from tests.api.test_ticket_actions import OPENER, OTHER, STAFF, login_as, open_ticket
from tests.test_files import PNG


def signed(url: str) -> dict[str, str]:
    """FakeS3 把簽章的參數放在網址裡。"""
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


async def create(env: Env, ticket_id: int, filename: str = "a.png", size: int = 10) -> Any:
    return await env.post(f"/tickets/{ticket_id}/attachments", {"filename": filename, "size": size})


async def stored(env: Env, attachment_id: int) -> TicketAttachment:
    async with env.sessionmaker() as db:
        attachment = await db.get(TicketAttachment, attachment_id)
        assert attachment is not None
        return attachment


async def test_single_upload(env: Env) -> None:
    ticket_id, _ = await open_ticket(env)
    response = await create(env, ticket_id, size=len(PNG))
    assert response.status_code == 201, response.json()
    body = response.json()
    attachment_id = body["attachment"]["id"]
    assert body["attachment"]["status"] == "pending"
    assert body["attachment"]["uploader_id"] == str(OPENER)
    assert (body["part_size"], body["part_count"]) == (None, None)
    # 簽章包含大小，上傳別的大小會被 S3 拒絕
    assert signed(body["url"]) == {"op": "put_object", "ContentLength": str(len(PNG))}

    url = f"/attachments/{attachment_id}/complete"
    # 還沒上傳
    assert (await env.post(url, {})).status_code == 409
    key = (await stored(env, attachment_id)).storage_key
    assert key == f"tickets/{ticket_id}/{attachment_id}/a.png"
    env.s3.objects[key] = PNG

    response = await env.post(url, {})
    assert response.status_code == 200
    assert (await stored(env, attachment_id)).uploaded_at is not None
    env.verify.assert_awaited_once_with(attachment_id)
    # 重複呼叫不會再檢查一次
    assert (await env.post(url, {})).status_code == 200
    env.verify.assert_awaited_once()


async def test_multipart_upload(env: Env) -> None:
    ticket_id, _ = await open_ticket(env)
    size = PART_SIZE * 2 + 5
    body = (await create(env, ticket_id, "video.mp4", size)).json()
    attachment_id = body["attachment"]["id"]
    assert (body["url"], body["part_size"], body["part_count"]) == (None, PART_SIZE, 3)

    parts_url = f"/attachments/{attachment_id}/parts"
    response = await env.post(parts_url, {"part_numbers": [1, 3]})
    assert response.status_code == 200
    urls = {item["part_number"]: signed(item["url"]) for item in response.json()}
    assert urls[1]["op"] == "upload_part"
    # 每一塊的大小都簽進網址，最後一塊比較小
    assert (urls[1]["ContentLength"], urls[3]["ContentLength"]) == (str(PART_SIZE), "5")
    assert (await env.post(parts_url, {"part_numbers": [4]})).status_code == 422

    attachment = await stored(env, attachment_id)
    assert attachment.upload_id is not None
    parts = env.s3.uploads[attachment.upload_id]
    parts[1] = b"a" * PART_SIZE
    parts[3] = b"c" * 5
    complete_url = f"/attachments/{attachment_id}/complete"
    # 少一塊
    assert (await env.post(complete_url, {})).status_code == 409
    # 大小不對
    parts[2] = b"b" * 10
    assert (await env.post(complete_url, {})).status_code == 409
    parts[2] = b"b" * PART_SIZE
    assert (await env.post(complete_url, {})).status_code == 200
    assert attachment.storage_key is not None
    assert env.s3.objects[attachment.storage_key] == parts_data(size)
    env.verify.assert_awaited_once_with(attachment_id)
    # 上傳完之後就不能再取得網址
    assert (await env.post(parts_url, {"part_numbers": [1]})).status_code == 409


def parts_data(size: int) -> bytes:
    return b"a" * PART_SIZE + b"b" * PART_SIZE + b"c" * (size - 2 * PART_SIZE)


async def test_upload_rules(env: Env) -> None:
    ticket_id, _ = await open_ticket(env)
    assert (await create(env, ticket_id, "a.exe")).status_code == 422
    assert (await create(env, ticket_id, size=1024**3 + 1)).status_code == 413
    assert (await create(env, ticket_id, size=0)).status_code == 422
    # 看不到的客服單
    await login_as(env, OTHER)
    assert (await create(env, ticket_id)).status_code == 404


async def test_only_uploader_can_use_it(env: Env) -> None:
    ticket_id, _ = await open_ticket(env)
    attachment_id = (await create(env, ticket_id, size=PART_SIZE + 1)).json()["attachment"]["id"]
    # 客服看得到這張單，但不是上傳的人
    await login_as(env, STAFF)
    for url in ("parts", "complete"):
        response = await env.post(f"/attachments/{attachment_id}/{url}", {"part_numbers": [1]})
        assert response.status_code == 404
    assert (await env.delete(f"/attachments/{attachment_id}")).status_code == 404


async def test_unsent_limit_and_remove(env: Env) -> None:
    ticket_id, _ = await open_ticket(env)
    ids = [(await create(env, ticket_id)).json()["attachment"]["id"] for _ in range(10)]
    assert (await create(env, ticket_id)).status_code == 409

    # 單一上傳：刪除檔案；分塊上傳到一半：取消 multipart upload
    first = await stored(env, ids[0])
    env.s3.objects[first.storage_key or ""] = PNG
    assert (await env.delete(f"/attachments/{ids[0]}")).status_code == 204
    assert first.storage_key not in env.s3.objects
    assert await stored_or_none(env, ids[0]) is None
    big = (await create(env, ticket_id, size=PART_SIZE + 1)).json()["attachment"]["id"]
    upload_id = (await stored(env, big)).upload_id
    assert (await env.delete(f"/attachments/{big}")).status_code == 204
    assert upload_id not in env.s3.uploads


async def stored_or_none(env: Env, attachment_id: int) -> TicketAttachment | None:
    async with env.sessionmaker() as db:
        return await db.get(TicketAttachment, attachment_id)


async def test_closed_ticket(env: Env) -> None:
    ticket_id, _ = await open_ticket(env)
    attachment_id = (await create(env, ticket_id)).json()["attachment"]["id"]
    assert (await env.post(f"/tickets/{ticket_id}/close", {})).status_code == 200
    assert (await create(env, ticket_id)).status_code == 409
    assert (await env.post(f"/attachments/{attachment_id}/complete", {})).status_code == 409
