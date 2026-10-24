import asyncio
import logging
from collections.abc import Callable
from functools import lru_cache

from redis.asyncio import Redis
from redis.exceptions import RedisError

from bot.core.config import get_settings

log = logging.getLogger(__name__)

# worker → bot 的事件通知，內容是 ticket_event.id
EVENTS_CHANNEL = "ticket-bot:events"


@lru_cache
def get_redis() -> Redis:
    # health_check_interval：pub/sub 長時間沒有訊息時，也能發現連線已經中斷
    return Redis.from_url(get_settings().redis_url, decode_responses=True, health_check_interval=30)


@lru_cache
def get_session_redis() -> Redis:
    """主要 API 的 session store，只能讀取 session、讀取並刪除授權碼（docs/auth.md 4.5）。"""
    return Redis.from_url(get_settings().session_redis_url, decode_responses=True)


async def publish_event(event_id: int) -> None:
    await get_redis().publish(EVENTS_CHANNEL, str(event_id))


async def listen_events(handler: Callable[[int], None]) -> None:
    """訂閱事件通知，斷線時自動重新連線。

    斷線期間的通知會遺失；瀏覽器重新連線時會從 ticket_event 補送。
    """
    while True:
        try:
            async with get_redis().pubsub() as pubsub:
                await pubsub.subscribe(EVENTS_CHANNEL)
                async for message in pubsub.listen():
                    if message["type"] == "message":
                        handler(int(message["data"]))
        except RedisError:
            log.exception("Event subscription failed, reconnecting in 5 seconds")
            await asyncio.sleep(5)
