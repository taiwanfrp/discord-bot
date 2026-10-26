import asyncio
import contextlib
import logging
import pkgutil
import signal

import discord
from discord import app_commands
from discord.ext import commands

from bot import cogs
from bot.api.app import Server, create_server
from bot.api.sse import EventHub
from bot.core.instance_lock import InstanceLock
from bot.core.redis import get_redis, listen_events
from bot.core.tree import CommandTree
from bot.db.engine import get_engine, get_sessionmaker
from bot.storage.s3 import s3_client
from bot.tickets.controls import TicketControlsView
from bot.tickets.panel import PanelView
from bot.tickets.rating import RateButton
from bot.worker.broker import broker

log = logging.getLogger(__name__)


class Bot(commands.Bot):
    def __init__(self) -> None:
        # members、message_content 是特權 intent，要在 Developer Portal 開啟
        intents = discord.Intents.default()
        intents.members = True
        # 沒有它的話，客服單頻道裡的訊息收到時內容與附件都是空的
        intents.message_content = True
        super().__init__(
            command_prefix=commands.when_mentioned,
            help_command=None,
            intents=intents,
            tree_cls=CommandTree,
            allowed_contexts=app_commands.AppCommandContext(guild=True, dm_channel=True),
        )
        self._events_task: asyncio.Task[None] | None = None
        self._api_server: Server | None = None
        self._api_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        # API 一直使用的 S3 client，關閉時一起關閉
        self._resources = contextlib.AsyncExitStack()
        # 把事件推送給連線中的 SSE
        self.event_hub = EventHub(get_sessionmaker)
        self._instance_lock: InstanceLock | None = None
        # 鎖被其他 bot 拿走時是 True，main.py 會用非 0 的狀態碼結束
        self.lost_instance_lock = False

    async def start(self, token: str, *, reconnect: bool = True) -> None:
        # 同一時間只能有一個 bot 連著 Discord（docs/tickets.md 11.3），取得鎖之後才登入。
        # 等待期間收到 SIGTERM 會直接結束，這時還沒有需要關閉的東西
        self._instance_lock = InstanceLock(get_redis(), "bot", on_lost=self._on_instance_lock_lost)
        await self._instance_lock.acquire()
        # 部署平台用 SIGTERM 停止服務，要正常關閉 API、queue 與資料庫連線，最後釋放鎖
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, self._on_sigterm)
        await super().start(token, reconnect=reconnect)
        # Discord 的連線一關閉 start() 就會回傳，接著 asyncio.run 會取消還沒做完的 task。
        # SIGTERM 時 close() 在另一個 task 裡，要等它做完（最後才釋放鎖）
        if self._close_task is not None:
            await self._close_task

    async def setup_hook(self) -> None:
        # 讓 bot 可以把工作放進 queue
        await broker.startup()
        # worker 產生的事件，在 cog 裡用 on_ticket_event(event_id) 接收
        self._events_task = asyncio.create_task(listen_events(self._on_ticket_event))

        # persistent view：bot 重啟後，已經送出的面板還能使用
        self.add_view(PanelView())
        # claimed=None：註冊所有按鈕（接手與釋放）
        self.add_view(TicketControlsView(claimed=None))
        # 關單私訊裡的評分按鈕（custom_id 帶客服單 id）
        self.add_dynamic_items(RateButton)

        for module in pkgutil.iter_modules(cogs.__path__, f"{cogs.__name__}."):
            await self.load_extension(module.name)
            log.info("Loaded %s", module.name)

        synced = await self.tree.sync()
        log.info("Synced %d global commands", len(synced))

        s3 = await self._resources.enter_async_context(s3_client())
        self._api_server = create_server(self, self.event_hub, s3)
        self._api_task = asyncio.create_task(self._api_server.serve())

    def _on_ticket_event(self, event_id: int) -> None:
        """bot 與 worker 寫入的 ticket_event（bot/db/hooks.py 透過 Redis pub/sub 通知）。"""
        self.event_hub.publish(event_id)
        self.dispatch("ticket_event", event_id)

    def _on_sigterm(self) -> None:
        self._shutdown("Received SIGTERM")

    def _on_instance_lock_lost(self) -> None:
        self.lost_instance_lock = True
        self._shutdown("Lost the bot lock")

    def _shutdown(self, reason: str) -> None:
        # 可能收到不只一次（例如 uv run 轉送、或是送給整個 process group），
        # 同時關閉兩次的話，第二次會中斷第一次正在關閉的連線
        if self._close_task is not None:
            return
        log.info("%s, shutting down", reason)
        self._close_task = asyncio.create_task(self.close())

    async def close(self) -> None:
        # SIGTERM 與 discord.py 結束時都會呼叫
        if self.is_closed():
            return
        # 先結束 SSE 的長連線，uvicorn 才不用等它們逾時
        self.event_hub.close()
        if self._api_server is not None and self._api_task is not None:
            self._api_server.should_exit = True
            await self._api_task
        if self._events_task is not None:
            self._events_task.cancel()
        await self._resources.aclose()
        await broker.shutdown()
        await get_engine().dispose()
        await super().close()
        # Discord 的連線關閉之後才釋放，下一個 bot 才不會在這之前連上
        if self._instance_lock is not None:
            await self._instance_lock.release()

    async def on_ready(self) -> None:
        log.info("We have logged in as %s", self.user)
