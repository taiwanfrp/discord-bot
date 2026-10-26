import asyncio
import inspect
import os
import signal
from collections.abc import AsyncIterator, Callable
from unittest.mock import AsyncMock, MagicMock

import pytest
from discord.ext import commands
from fakeredis import FakeAsyncRedis
from redis.exceptions import ConnectionError
from taskiq import TaskiqScheduler

from bot.core import bot as bot_module
from bot.core import instance_lock
from bot.core.instance_lock import InstanceLock
from bot.worker import scheduler as scheduler_module

KEY = "ticket-bot:lock:bot"


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    # 測試不等那麼久
    monkeypatch.setattr(instance_lock, "TIMEOUT", 0.5)
    monkeypatch.setattr(instance_lock, "RENEW_INTERVAL", 0.05)
    monkeypatch.setattr(instance_lock, "RETRY_INTERVAL", 0.01)


@pytest.fixture
async def sigterm() -> AsyncIterator[None]:
    """bot 與 scheduler 取得鎖之後會註冊 SIGTERM，測試完拿掉。"""
    yield
    asyncio.get_running_loop().remove_signal_handler(signal.SIGTERM)


@pytest.fixture
def redis() -> FakeAsyncRedis:
    return FakeAsyncRedis()


async def until(condition: Callable[[], object]) -> None:
    """等到 condition() 成立（可以是 async 函式），最多 2 秒。"""
    async with asyncio.timeout(2):
        while True:
            result = condition()
            if inspect.isawaitable(result):
                result = await result
            if result:
                return
            await asyncio.sleep(0.01)


async def test_wait_for_the_running_one(redis: FakeAsyncRedis) -> None:
    first = InstanceLock(redis, "bot", on_lost=MagicMock())
    await first.acquire()
    second = InstanceLock(redis, "bot", on_lost=MagicMock())
    waiting = asyncio.create_task(second.acquire())
    # 一直延長，所以超過有效期限也不會被拿走
    await asyncio.sleep(1)
    assert not waiting.done()

    await first.release()
    await asyncio.wait_for(waiting, 1)
    await second.release()
    assert await redis.get(KEY) is None


async def test_crashed(redis: FakeAsyncRedis) -> None:
    # 持有鎖的程序當機了（沒有延長，也沒有釋放）：過期之後就拿得到
    await redis.set(KEY, "crashed", px=300)
    lock = InstanceLock(redis, "bot", on_lost=MagicMock())
    await asyncio.wait_for(lock.acquire(), 1)
    await lock.release()


async def test_lost(redis: FakeAsyncRedis) -> None:
    on_lost = MagicMock()
    lock = InstanceLock(redis, "bot", on_lost=on_lost)
    await lock.acquire()
    # 例如這個程序停住太久，鎖過期之後被另一個拿走
    await redis.set(KEY, "other")
    await until(lambda: on_lost.called)
    await asyncio.sleep(0.2)
    on_lost.assert_called_once()
    # 不會刪掉別人的鎖
    await lock.release()
    assert await redis.get(KEY) == b"other"


async def test_missing(redis: FakeAsyncRedis) -> None:
    on_lost = MagicMock()
    lock = InstanceLock(redis, "bot", on_lost=on_lost)
    await lock.acquire()
    token = await redis.get(KEY)
    # Redis 重新啟動，鎖不見了：還沒有被拿走，就拿回來
    await redis.delete(KEY)
    await until(lambda: redis.exists(KEY))
    assert await redis.get(KEY) == token
    on_lost.assert_not_called()
    await lock.release()


async def test_redis_down(monkeypatch: pytest.MonkeyPatch, redis: FakeAsyncRedis) -> None:
    on_lost = MagicMock()
    lock = InstanceLock(redis, "bot", on_lost=on_lost)
    await lock.acquire()
    reacquire = lock._lock.reacquire
    failures = 0

    async def flaky() -> bool:
        nonlocal failures
        if failures < 5:
            failures += 1
            raise ConnectionError("down")
        return await reacquire()

    monkeypatch.setattr(lock._lock, "reacquire", flaky)
    await until(lambda: failures == 5)
    # 連上之後繼續延長，不會停止服務
    await asyncio.sleep(1)
    on_lost.assert_not_called()
    assert await redis.exists(KEY)
    await lock.release()


@pytest.fixture
async def bot(
    monkeypatch: pytest.MonkeyPatch, redis: FakeAsyncRedis, sigterm: None
) -> tuple[bot_module.Bot, asyncio.Task[None]]:
    """回傳 bot 與執行 start() 的 task（main.py 的 bot.run()）。"""
    monkeypatch.setattr(bot_module, "get_redis", lambda: redis)
    # 不連 Discord。跟 discord.py 一樣：連線一關閉 start() 就回傳，
    # 但 close() 在那之後還有其他步驟（關閉 HTTP session）
    disconnected = asyncio.Event()

    async def connect(*_: object, **__: object) -> None:
        await disconnected.wait()

    async def disconnect(*_: object) -> None:
        disconnected.set()
        await asyncio.sleep(0.05)

    monkeypatch.setattr(commands.Bot, "start", connect)
    monkeypatch.setattr(commands.Bot, "close", disconnect)
    bot = bot_module.Bot()
    main = asyncio.create_task(bot.start("token"))
    await until(lambda: redis.exists(KEY))
    return bot, main


async def test_bot_sigterm(
    bot: tuple[bot_module.Bot, asyncio.Task[None]], redis: FakeAsyncRedis
) -> None:
    client, main = bot
    await asyncio.sleep(0.05)
    assert signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL
    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(main, 2)
    # start() 回傳之後 asyncio.run 會取消其他還沒做完的 task，所以這時鎖要已經釋放
    assert not await redis.exists(KEY)
    assert not client.lost_instance_lock


async def test_bot_lost(
    bot: tuple[bot_module.Bot, asyncio.Task[None]], redis: FakeAsyncRedis
) -> None:
    client, main = bot
    # 另一個 bot 拿走了鎖：關閉，main.py 用非 0 的狀態碼結束
    await redis.set(KEY, "other")
    await asyncio.wait_for(main, 2)
    assert client.lost_instance_lock
    assert await redis.get(KEY) == b"other"


async def run_scheduler(scheduler: scheduler_module.Scheduler) -> object:
    """跟 taskiq 的 run_scheduler 一樣：取消 main task 時呼叫 shutdown。回傳結束的狀態碼。"""
    await scheduler.startup()
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        try:
            await scheduler.shutdown()
        except SystemExit as error:
            return error.code
    return None


async def exit_code(main: asyncio.Task[object]) -> object:
    # 不能用 wait_for：逾時的時候它會取消 main task，跟 SIGTERM 一樣會走到正常關閉的流程
    await asyncio.wait([main], timeout=1)
    assert main.done(), "scheduler 沒有停止"
    return main.result()


@pytest.fixture
def scheduler(
    monkeypatch: pytest.MonkeyPatch, redis: FakeAsyncRedis, sigterm: None
) -> scheduler_module.Scheduler:
    monkeypatch.setattr(scheduler_module, "get_redis", lambda: redis)
    # 不連 broker
    monkeypatch.setattr(TaskiqScheduler, "startup", AsyncMock())
    monkeypatch.setattr(TaskiqScheduler, "shutdown", AsyncMock())
    return scheduler_module.Scheduler(MagicMock(), sources=[])


async def test_scheduler_sigterm(
    scheduler: scheduler_module.Scheduler, redis: FakeAsyncRedis
) -> None:
    main = asyncio.create_task(run_scheduler(scheduler))
    await until(lambda: redis.exists("ticket-bot:lock:scheduler"))
    await asyncio.sleep(0.05)
    # 沒有註冊的話，送出 SIGTERM 會結束整個 pytest
    assert signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL
    # 收到 SIGTERM 時正常關閉並釋放鎖，下一個 scheduler 不用等鎖過期
    os.kill(os.getpid(), signal.SIGTERM)
    assert await exit_code(main) is None
    assert not await redis.exists("ticket-bot:lock:scheduler")


async def test_scheduler_lost(scheduler: scheduler_module.Scheduler, redis: FakeAsyncRedis) -> None:
    main = asyncio.create_task(run_scheduler(scheduler))
    await until(lambda: redis.exists("ticket-bot:lock:scheduler"))
    await redis.set("ticket-bot:lock:scheduler", "other")
    assert await exit_code(main) == 1
