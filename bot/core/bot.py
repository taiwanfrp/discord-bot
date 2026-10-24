import asyncio
import logging
import pkgutil

import discord
from discord import app_commands
from discord.ext import commands

from bot import cogs
from bot.core.redis import listen_events
from bot.core.tree import CommandTree
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

    async def setup_hook(self) -> None:
        # 讓 bot 可以把工作放進 queue
        await broker.startup()
        # worker 產生的事件，在 cog 裡用 on_ticket_event(event_id) 接收
        self._events_task = asyncio.create_task(
            listen_events(lambda event_id: self.dispatch("ticket_event", event_id))
        )

        for module in pkgutil.iter_modules(cogs.__path__, f"{cogs.__name__}."):
            await self.load_extension(module.name)
            log.info("Loaded %s", module.name)

        synced = await self.tree.sync()
        log.info("Synced %d global commands", len(synced))

    async def close(self) -> None:
        if self._events_task is not None:
            self._events_task.cancel()
        await broker.shutdown()
        await super().close()

    async def on_ready(self) -> None:
        log.info("We have logged in as %s", self.user)
