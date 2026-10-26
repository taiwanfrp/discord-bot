import asyncio
import signal

from taskiq import TaskiqScheduler
from taskiq.schedule_sources import LabelScheduleSource

from bot.core.instance_lock import InstanceLock
from bot.core.redis import get_redis
from bot.worker.broker import broker, schedule_source


class Scheduler(TaskiqScheduler):
    """同一時間只能有一個 scheduler（docs/tickets.md 11.3），取得鎖之後才開始放入工作。"""

    _instance_lock: InstanceLock | None = None
    _lost_lock = False

    async def startup(self) -> None:
        # taskiq 在 main task 裡呼叫 startup；取消 main task 會跟 Ctrl+C 一樣，
        # 走正常關閉的流程（呼叫 shutdown）
        main = asyncio.current_task()
        assert main is not None

        def on_lost() -> None:
            self._lost_lock = True
            main.cancel()

        self._instance_lock = InstanceLock(get_redis(), "scheduler", on_lost=on_lost)
        # 等待期間收到 SIGTERM 會直接結束，這時還沒有需要關閉的東西
        await self._instance_lock.acquire()
        # taskiq 沒有處理 SIGTERM，直接結束的話鎖不會被釋放，下一個 scheduler 要等鎖過期
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, main.cancel)
        await super().startup()

    async def shutdown(self) -> None:
        await super().shutdown()
        if self._instance_lock is not None:
            await self._instance_lock.release()
        if self._lost_lock:
            # 另一個 scheduler 拿走了鎖，用非 0 的狀態碼表示不正常結束
            raise SystemExit(1)


scheduler = Scheduler(broker, sources=[LabelScheduleSource(broker), schedule_source])
