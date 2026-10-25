import logging

from fastapi import Request
from redis.asyncio import Redis
from redis.exceptions import RedisError

log = logging.getLogger(__name__)


def client_ip(request: Request) -> str:
    # uvicorn 已經依照 FORWARDED_ALLOW_IPS 處理過 X-Forwarded-For
    return request.client.host if request.client else "unknown"


async def allow(redis: Redis, key: str, *, limit: int, window: int) -> bool:
    """固定時間窗的計數：每 window 秒最多 limit 次。

    Redis 無法使用時放行：這是為了擋大量請求，不是安全檢查（安全靠 token 本身）。
    """
    try:
        async with redis.pipeline(transaction=True) as pipe:
            # 第一次才設定期限；INCR 不會改變期限
            pipe.set(key, 0, ex=window, nx=True)
            pipe.incr(key)
            _, count = await pipe.execute()
    except RedisError:
        log.warning("Rate limit check failed", exc_info=True)
        return True
    return int(count) <= limit
