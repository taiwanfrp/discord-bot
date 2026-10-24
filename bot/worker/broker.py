import datetime
from typing import Annotated

import discord
from taskiq import TaskiqDepends, TaskiqEvents, TaskiqMessage, TaskiqState
from taskiq.middlewares import SmartRetryMiddleware
from taskiq.serializers import JSONSerializer
from taskiq_redis import ListRedisScheduleSource, RedisStreamBroker

from bot.core.config import get_settings
from bot.db.engine import get_engine


class ScheduleSource(ListRedisScheduleSource):
    """延遲執行的工作（重試）存在這裡，由 scheduler 到時間後放進 queue。"""

    def _parse_time_key(self, key: str) -> datetime.datetime | None:
        # 原本的實作用 key.split(":", 2) 取時間，prefix 含有 ":" 時會解析失敗，
        # scheduler 就讀不到已經過了時間的 key（例如 scheduler 重啟期間的重試）
        try:
            return datetime.datetime.strptime(
                key.removeprefix(f"{self._prefix}:time:"), "%Y-%m-%dT%H:%M"
            ).replace(tzinfo=datetime.UTC)
        except ValueError:
            return None


class RetryMiddleware(SmartRetryMiddleware):
    """失敗時重試，間隔是指數退避：1、2、4、8、16 分鐘。

    SmartRetryMiddleware 的 use_delay_exponent 其實是線性增加，所以自己計算。
    """

    def make_delay(self, message: TaskiqMessage, retries: int) -> float:
        delay: float = self.default_delay * 2 ** (retries - 1)
        return min(delay, self.max_delay_exponent)


settings = get_settings()

schedule_source = ScheduleSource(
    settings.redis_url, prefix="ticket-bot:schedule", serializer=JSONSerializer()
)

broker = RedisStreamBroker(
    settings.redis_url,
    queue_name="ticket-bot:queue",
    # 注意：taskiq-redis 另外會用 autoclaim:{consumer_group_name}:{queue_name} 當作鎖的 key
    consumer_group_name="ticket-bot:worker",
    # 建立 consumer group 時從頭讀取，第一次部署前放進去的工作才不會被略過
    consumer_id="0",
    # worker 停止時，已經取出但還沒執行的工作要等 idle_timeout 之後才會被其他 worker 接手，
    # 所以一次只取一個
    xread_count=1,
    # 工作超過這個時間沒有 ack，就會被其他 worker 接手重新執行，所以要比最長的工作還長
    idle_timeout=30 * 60 * 1000,
    unacknowledged_batch_size=10,
    # worker 在持有鎖的時候被停止，鎖也會自動釋放
    unacknowledged_lock_timeout=30,
    # ack 之後訊息還會留在 stream 裡，超過這個數量就刪掉舊的
    maxlen=10_000,
).with_middlewares(
    RetryMiddleware(
        default_retry_count=6,
        default_retry_label=True,
        default_delay=60,
        max_delay_exponent=60 * 60,
        schedule_source=schedule_source,
    )
)


@broker.on_event(TaskiqEvents.WORKER_STARTUP)
async def _worker_startup(state: TaskiqState) -> None:
    # worker 只使用 Discord 的 REST API，不連 gateway
    # （沒有 gateway，intents 沒有作用；用 default 只是避免 discord.py 警告）
    state.discord = discord.Client(intents=discord.Intents.default())
    await state.discord.login(settings.discord_token.get_secret_value())


@broker.on_event(TaskiqEvents.WORKER_SHUTDOWN)
async def _worker_shutdown(state: TaskiqState) -> None:
    await state.discord.close()
    await get_engine().dispose()


def _get_discord(state: Annotated[TaskiqState, TaskiqDepends()]) -> discord.Client:
    client: discord.Client = state.discord
    return client


# 工作的參數：`discord_client: DiscordClient`（不要叫 discord，會蓋掉 discord 模組）
DiscordClient = Annotated[discord.Client, TaskiqDepends(_get_discord)]
