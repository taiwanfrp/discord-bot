"""SSE 的事件分送（docs/tickets.md 8.5）。

bot 從 Redis pub/sub 收到 ticket_event 的 id 後呼叫 EventHub.publish，EventHub 讀取那筆事件，
分送給連線中的 SSE。斷線期間的事件，瀏覽器重新連線時用 Last-Event-ID 從 ticket_event 補送。
"""

import asyncio
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db.models import TicketEvent

log = logging.getLogger(__name__)

# 每個連線最多累積幾個還沒送出的事件；超過就中斷連線，讓瀏覽器重新連線後補送
QUEUE_SIZE = 1000
# 客服單列表（GET /events）只推送客服單本身的變動（新開單、被接手、已關閉等）
LIST_EVENT_PREFIX = "ticket."


@dataclass(eq=False)
class Subscription:
    # None 表示客服單列表
    ticket_id: int | None
    # None 表示連線要結束
    queue: asyncio.Queue[TicketEvent | None] = field(
        default_factory=lambda: asyncio.Queue(QUEUE_SIZE)
    )

    def wants(self, event: TicketEvent) -> bool:
        if self.ticket_id is None:
            return event.type.startswith(LIST_EVENT_PREFIX)
        return event.ticket_id == self.ticket_id

    def end(self) -> None:
        # 佇列滿的時候也要能放入結束的訊號
        while not self.queue.empty():
            self.queue.get_nowait()
        self.queue.put_nowait(None)


class EventHub:
    def __init__(self, sessionmaker: Callable[[], async_sessionmaker[AsyncSession]]) -> None:
        self._sessionmaker = sessionmaker
        self._subscriptions: set[Subscription] = set()
        self._ids: asyncio.Queue[int] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None

    def publish(self, event_id: int) -> None:
        if not self._subscriptions:
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())
        self._ids.put_nowait(event_id)

    @contextmanager
    def subscribe(self, ticket_id: int | None) -> Iterator[Subscription]:
        subscription = Subscription(ticket_id)
        self._subscriptions.add(subscription)
        try:
            yield subscription
        finally:
            self._subscriptions.discard(subscription)

    def close(self) -> None:
        """關閉所有連線（bot 關閉時）。"""
        for subscription in self._subscriptions:
            subscription.end()
        if self._task is not None:
            self._task.cancel()

    async def _run(self) -> None:
        # 只有一個工作依序處理，事件才不會亂序
        while True:
            event_id = await self._ids.get()
            try:
                async with self._sessionmaker()() as db:
                    event = await db.get(TicketEvent, event_id)
            except Exception:
                log.exception("Failed to load ticket event %d", event_id)
                continue
            if event is None:
                continue
            for subscription in list(self._subscriptions):
                if not subscription.wants(event):
                    continue
                try:
                    subscription.queue.put_nowait(event)
                except asyncio.QueueFull:
                    log.warning("SSE subscriber is too slow, disconnecting it")
                    subscription.end()
                    self._subscriptions.discard(subscription)
