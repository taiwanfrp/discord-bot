from datetime import UTC, datetime

from bot.db.models import AttachmentStatus, Source, Ticket, TicketAttachment, TicketCategory
from bot.storage.share import new_share_token
from tests.api.helpers import Env
from tests.api.test_attachments import signed


async def shared(env: Env, filename: str = "圖.png", content_type: str = "image/png") -> str:
    """建立一個已經分享的附件，回傳分享連結的路徑。"""
    token, token_hash = new_share_token()
    async with env.sessionmaker() as db:
        category = TicketCategory(name="帳務")
        db.add(category)
        await db.flush()
        ticket = Ticket(
            category_id=category.id, source=Source.WEB, opener_id=5, subject="", content=""
        )
        db.add(ticket)
        await db.flush()
        attachment = TicketAttachment(
            ticket_id=ticket.id,
            uploader_id=5,
            source=Source.WEB,
            storage_key="tickets/1/1/a.png",
            filename=filename,
            content_type=content_type,
            size=1,
            status=AttachmentStatus.DONE,
            share_token_hash=token_hash,
        )
        db.add(attachment)
        await db.commit()
        return f"/a/{attachment.id}/{token}"


async def update(env: Env, **values: object) -> None:
    async with env.sessionmaker() as db:
        attachment = await db.get_one(TicketAttachment, 1)
        for name, value in values.items():
            setattr(attachment, name, value)
        await db.commit()


async def test_redirects_to_presigned_url(env: Env) -> None:
    path = await shared(env)
    # 不需要登入
    response = await env.client.get(path)
    assert response.status_code == 302
    assert response.headers["cache-control"] == "no-store"
    params = signed(response.headers["location"])
    assert params["op"] == "get_object"
    assert params["ResponseContentType"] == "image/png"
    assert params["ResponseContentDisposition"].startswith("inline;")
    # Discord 抓取預覽時可能先送 HEAD
    assert (await env.client.head(path)).status_code == 302

    # 不是圖片、影片的檔案一律下載
    await update(env, content_type="application/pdf")
    params = signed((await env.client.get(path)).headers["location"])
    assert params["ResponseContentDisposition"].startswith("attachment;")


async def test_invalid_links(env: Env) -> None:
    path = await shared(env)
    attachment_id, token = path.split("/")[2:]
    assert (await env.client.get(f"/a/{attachment_id}/{token[:-1]}x")).status_code == 404
    assert (await env.client.get(f"/a/999/{token}")).status_code == 404
    await update(env, status=AttachmentStatus.PENDING)
    assert (await env.client.get(path)).status_code == 404
    await update(env, status=AttachmentStatus.DONE, share_revoked_at=datetime.now(UTC))
    assert (await env.client.get(path)).status_code == 404


async def test_rate_limit(env: Env) -> None:
    path = await shared(env)
    for _ in range(60):
        assert (await env.client.get(path)).status_code == 302
    assert (await env.client.get(path)).status_code == 429
