from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from bot.api.deps import DBDep, RedisDep, S3Dep
from bot.api.ratelimit import allow, client_ip
from bot.core.config import get_settings
from bot.db.models import AttachmentStatus, DiscordMedia, MediaKind
from bot.storage.media import discord_url
from bot.storage.share import media_url

router = APIRouter()

# 每個 IP 每分鐘最多幾次；一頁訊息裡可能有很多不同的表情
RATE_LIMIT = 600
# 還沒存到 S3 時轉址到 Discord，快取短一點，存好之後才會改用 S3
DISCORD_MAX_AGE = 60


async def redirect(
    kind: MediaKind,
    media_id: int,
    extension: str,
    request: Request,
    db: DBDep,
    redis: RedisDep,
    s3: S3Dep,
) -> RedirectResponse:
    key = f"ticket-bot:ratelimit:media:{client_ip(request)}"
    if not await allow(redis, key, limit=RATE_LIMIT, window=60):
        raise HTTPException(429, "Too many requests")
    media = await db.get(DiscordMedia, media_id)
    if media is None or media.kind is not kind or media.status is not AttachmentStatus.DONE:
        # 還沒存到 S3（最多等 1 分鐘）、已經取不到，或是這個功能之前的訊息
        return RedirectResponse(
            discord_url(kind, media_id, extension),
            status_code=302,
            headers={"Cache-Control": f"private, max-age={DISCORD_MAX_AGE}"},
        )
    # 快取的時間要比 presigned URL 的有效期限短
    max_age = get_settings().s3_presign_ttl // 2
    return RedirectResponse(
        await media_url(s3, media),
        status_code=302,
        headers={"Cache-Control": f"private, max-age={max_age}"},
    )


@router.get("/emojis/{emoji_id}.{extension}", status_code=302)
async def emoji(
    emoji_id: int,
    extension: Literal["gif", "webp"],
    request: Request,
    db: DBDep,
    redis: RedisDep,
    s3: S3Dep,
) -> RedirectResponse:
    """自訂表情的圖片（docs/tickets.md 8.7）。不需要登入，表情在 Discord 上本來就是公開的。"""
    return await redirect(MediaKind.EMOJI, emoji_id, extension, request, db, redis, s3)


@router.get("/stickers/{sticker_id}.{extension}", status_code=302)
async def sticker(
    sticker_id: int,
    extension: Literal["png", "gif", "json"],
    request: Request,
    db: DBDep,
    redis: RedisDep,
    s3: S3Dep,
) -> RedirectResponse:
    """貼圖（docs/tickets.md 8.7），跟表情一樣不需要登入。"""
    return await redirect(MediaKind.STICKER, sticker_id, extension, request, db, redis, s3)
