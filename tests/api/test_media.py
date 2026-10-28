import pytest

from bot.api.routes import media
from bot.core.config import get_settings
from bot.db.models import AttachmentStatus, DiscordMedia, MediaKind
from tests.api.helpers import Env
from tests.api.test_attachments import signed


async def add_media(
    env: Env,
    media_id: int,
    kind: MediaKind,
    extension: str,
    status: AttachmentStatus = AttachmentStatus.DONE,
) -> None:
    async with env.sessionmaker() as db:
        key = f"{kind.value}s/{media_id}.{extension}" if status is AttachmentStatus.DONE else None
        db.add(
            DiscordMedia(
                id=media_id, kind=kind, extension=extension, status=status, storage_key=key
            )
        )
        await db.commit()


async def test_redirects_to_s3(env: Env) -> None:
    await add_media(env, 1, MediaKind.EMOJI, "gif")
    await add_media(env, 2, MediaKind.STICKER, "png")
    # 不需要登入
    response = await env.client.get("/emojis/1.gif")
    assert response.status_code == 302
    assert response.headers["location"].startswith("https://s3.test/emojis/1.gif?")
    assert signed(response.headers["location"])["ResponseContentType"] == "image/gif"
    # 快取的時間比 presigned URL 的有效期限短
    max_age = get_settings().s3_presign_ttl // 2
    assert response.headers["cache-control"] == f"private, max-age={max_age}"

    response = await env.client.get("/stickers/2.png")
    assert signed(response.headers["location"])["ResponseContentType"] == "image/png"


async def test_falls_back_to_discord(env: Env) -> None:
    await add_media(env, 1, MediaKind.EMOJI, "webp", AttachmentStatus.PENDING)
    await add_media(env, 2, MediaKind.STICKER, "gif")
    cases = {
        # 還沒存到 S3
        "/emojis/1.webp": "https://cdn.discordapp.com/emojis/1.webp",
        # 這個功能之前的訊息
        "/emojis/3.gif": "https://cdn.discordapp.com/emojis/3.gif",
        "/stickers/4.png": "https://cdn.discordapp.com/stickers/4.png",
        # GIF 的貼圖在另一個網域
        "/stickers/5.gif": "https://media.discordapp.net/stickers/5.gif",
        # 貼圖的 id 不會當成表情
        "/emojis/2.gif": "https://cdn.discordapp.com/emojis/2.gif",
    }
    for path, url in cases.items():
        response = await env.client.get(path)
        assert (response.status_code, response.headers["location"]) == (302, url)
        assert response.headers["cache-control"] == "private, max-age=60"


async def test_invalid_paths(env: Env) -> None:
    for path in ("/emojis/1.png", "/stickers/1.webp", "/emojis/abc.gif"):
        assert (await env.client.get(path)).status_code == 422
    assert (await env.client.get("/emojis/1")).status_code == 404


async def test_rate_limit(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(media, "RATE_LIMIT", 3)
    for _ in range(3):
        assert (await env.client.get("/emojis/1.gif")).status_code == 302
    assert (await env.client.get("/emojis/1.gif")).status_code == 429
