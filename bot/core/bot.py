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
from bot.core.redis import listen_events
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

    async def setup_hook(self) -> None:
        # 部署平台用 SIGTERM 停止服務，要正常關閉 API、queue 與資料庫連線
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, self._on_sigterm)

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
        # 可能收到不只一次（例如 uv run 轉送、或是送給整個 process group），
        # 同時關閉兩次的話，第二次會中斷第一次正在關閉的連線
        if self._close_task is not None:
            return
        log.info("Received SIGTERM, shutting down")
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

    async def on_ready(self) -> None:
        log.info("We have logged in as %s", self.user)
