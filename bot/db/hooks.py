"""commit 之後，把這次新增的 ticket_event 透過 Redis pub/sub 通知 bot（docs/tickets.md 8.5）。

bot 收到後推送 SSE。所以 ticket_event 一律用 ORM 新增（db.add），不要用 Core 的 insert，
否則這裡收不到。
"""

import asyncio
import logging
from typing import Any

from redis.exceptions import RedisError
from sqlalchemy import event
from sqlalchemy.orm import Session

from bot.core.redis import publish_event
from bot.db.models import TicketEvent

log = logging.getLogger(__name__)

_PENDING = "ticket_event_ids"
# 背景的推送工作，保留參照以免被 GC
_tasks: set[asyncio.Task[None]] = set()


@event.listens_for(Session, "after_flush")
def _collect(session: Session, _: Any) -> None:
    # after_flush 時 session.new 還是 flush 之前的內容，而且 id 已經產生
    ids = [obj.id for obj in session.new if isinstance(obj, TicketEvent)]
    if ids:
        session.info.setdefault(_PENDING, []).extend(ids)


@event.listens_for(Session, "after_commit")
def _publish(session: Session) -> None:
    ids: list[int] | None = session.info.pop(_PENDING, None)
    if ids:
        task = asyncio.get_running_loop().create_task(_send(ids))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)


@event.listens_for(Session, "after_rollback")
def _discard(session: Session) -> None:
    session.info.pop(_PENDING, None)


async def _send(ids: list[int]) -> None:
    for event_id in ids:
        try:
            await publish_event(event_id)
        except RedisError:
            # 瀏覽器重新連線時會從 ticket_event 補送，所以只記錄
            log.warning("Failed to publish ticket event %d", event_id, exc_info=True)
