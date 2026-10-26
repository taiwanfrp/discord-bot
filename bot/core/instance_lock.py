"""同一時間只能有一個在執行的服務（bot、scheduler）用的鎖（docs/tickets.md 11.3）。

部署平台先啟動新版本、再停止舊版本時，新的會等舊的停止、釋放鎖之後才開始執行。
"""

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Callable

from redis.asyncio import Redis
from redis.exceptions import LockError, LockNotOwnedError, RedisError

log = logging.getLogger(__name__)

# 鎖的有效期限（秒）：持有鎖的程序當機時，其他的最多等這麼久
TIMEOUT = 30
# 多久延長一次
RENEW_INTERVAL = 10
# 等待鎖、連不上 Redis 時，多久再試一次
RETRY_INTERVAL = 1


class InstanceLock:
    def __init__(self, redis: Redis, name: str, *, on_lost: Callable[[], None]) -> None:
        """on_lost：鎖被其他程序拿走時呼叫，要馬上停止服務。"""
        self.name = name
        self._lock = redis.lock(
            f"ticket-bot:lock:{name}", timeout=TIMEOUT, sleep=RETRY_INTERVAL, thread_local=False
        )
        self._token = uuid.uuid4().hex
        self._on_lost = on_lost
        self._task: asyncio.Task[None] | None = None

    async def acquire(self) -> None:
        """取得鎖之後才回傳，取不到就一直等。之後在背景定時延長。"""
        if not await self._lock.acquire(blocking=False, token=self._token):
            log.warning("Another %s is running, waiting for it to stop", self.name)
            await self._lock.acquire(token=self._token)
        log.info("Acquired the %s lock", self.name)
        self._task = asyncio.create_task(self._keep())

    async def release(self) -> None:
        if self._task is not None:
            self._task.cancel()
        # 已經被拿走，或是連不上 Redis（到期後會自動釋放）
        with contextlib.suppress(LockError, RedisError):
            await self._lock.release()

    async def _keep(self) -> None:
        failing = False
        while True:
            await asyncio.sleep(RETRY_INTERVAL if failing else RENEW_INTERVAL)
            try:
                owned = await self._renew()
            except RedisError:
                # 連不上 Redis 時，其他程序也拿不到鎖：繼續執行，連上之後再確認
                if not failing:
                    log.warning("Failed to renew the %s lock, retrying", self.name, exc_info=True)
                failing = True
                continue
            if not owned:
                log.error("Another %s took the lock, shutting down", self.name)
                self._on_lost()
                return
            if failing:
                log.info("Renewed the %s lock", self.name)
            failing = False

    async def _renew(self) -> bool:
        try:
            await self._lock.reacquire()
        except LockNotOwnedError:
            # 鎖不見了（例如 Redis 重新啟動，或是太久沒有延長而過期）：
            # 還沒有被其他程序拿走的話，用同一個 token 拿回來
            if not await self._lock.acquire(blocking=False, token=self._token):
                return False
            log.warning("The %s lock was missing, acquired it again", self.name)
        return True
