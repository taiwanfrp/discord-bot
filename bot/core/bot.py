import asyncio
import logging
import pkgutil
import signal

import discord
from discord import app_commands
from discord.ext import commands

from bot import cogs
from bot.api.app import Server, create_server
from bot.core.redis import listen_events
from bot.core.tree import CommandTree
from bot.db.engine import get_engine
from bot.tickets.panel import PanelView
from bot.worker.broker import broker

log = logging.getLogger(__name__)


class Bot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True
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

    async def setup_hook(self) -> None:
        # 部署平台用 SIGTERM 停止服務，要正常關閉 API、queue 與資料庫連線
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, self._on_sigterm)

        # 讓 bot 可以把工作放進 queue
        await broker.startup()
        # worker 產生的事件，在 cog 裡用 on_ticket_event(event_id) 接收
        self._events_task = asyncio.create_task(
            listen_events(lambda event_id: self.dispatch("ticket_event", event_id))
        )

        # persistent view：bot 重啟後，已經送出的面板還能使用
        self.add_view(PanelView())

        for module in pkgutil.iter_modules(cogs.__path__, f"{cogs.__name__}."):
            await self.load_extension(module.name)
            log.info("Loaded %s", module.name)

        synced = await self.tree.sync()
        log.info("Synced %d global commands", len(synced))

        self._api_server = create_server(self)
        self._api_task = asyncio.create_task(self._api_server.serve())

    def _on_sigterm(self) -> None:
        log.info("Received SIGTERM, shutting down")
        self._close_task = asyncio.create_task(self.close())

    async def close(self) -> None:
        # SIGTERM 與 discord.py 結束時都會呼叫
        if self.is_closed():
            return
        if self._api_server is not None and self._api_task is not None:
            self._api_server.should_exit = True
            await self._api_task
        if self._events_task is not None:
            self._events_task.cancel()
        await broker.shutdown()
        await get_engine().dispose()
        await super().close()

    async def on_ready(self) -> None:
        log.info("We have logged in as %s", self.user)
