from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from bot.api.deps import DBDep, RedisDep, S3Dep
from bot.api.ratelimit import allow, client_ip
from bot.db.models import TicketAttachment
from bot.storage.share import is_shared, presign_download

router = APIRouter()

# 每個 IP 每分鐘最多幾次
RATE_LIMIT = 60


# Discord 抓取預覽時可能先送 HEAD
@router.api_route("/a/{attachment_id}/{token}", methods=["GET", "HEAD"], status_code=302)
async def open_shared(
    attachment_id: int, token: str, request: Request, db: DBDep, redis: RedisDep, s3: S3Dep
) -> RedirectResponse:
    """分享連結（docs/tickets.md 8.4.1）：不需要登入，token 正確就轉址到新的 presigned URL。"""
    key = f"ticket-bot:ratelimit:share:{client_ip(request)}"
    if not await allow(redis, key, limit=RATE_LIMIT, window=60):
        raise HTTPException(429, "Too many requests")
    attachment = await db.get(TicketAttachment, attachment_id)
    # 不存在、token 錯誤、已經撤銷都回傳一樣的結果
    if attachment is None or not is_shared(attachment, token):
        raise HTTPException(404, "Not found")
    url = await presign_download(s3, attachment)
    # 每次都要轉址到新的網址，不能被快取
    return RedirectResponse(url, status_code=302, headers={"Cache-Control": "no-store"})
