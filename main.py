import logging
import os
import pkgutil

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from bot import cogs
from bot.core.tree import CommandTree

log = logging.getLogger(__name__)

load_dotenv()


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

    async def setup_hook(self) -> None:
        for module in pkgutil.iter_modules(cogs.__path__, f"{cogs.__name__}."):
            await self.load_extension(module.name)
            log.info("Loaded %s", module.name)

        synced = await self.tree.sync()
        log.info("Synced %d global commands", len(synced))

    async def on_ready(self) -> None:
        log.info("We have logged in as %s", self.user)


if __name__ == "__main__":
    Bot().run(os.environ["DISCORD_TOKEN"], root_logger=True)
