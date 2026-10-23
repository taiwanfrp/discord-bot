import os
import pkgutil

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from bot import cogs

load_dotenv()


class Bot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(
            command_prefix=commands.when_mentioned,
            help_command=None,
            intents=intents,
            allowed_contexts=app_commands.AppCommandContext(guild=True, dm_channel=True),
        )

    async def setup_hook(self) -> None:
        for module in pkgutil.iter_modules(cogs.__path__, f"{cogs.__name__}."):
            await self.load_extension(module.name)
            print(f"Loaded {module.name}")

        synced = await self.tree.sync()
        print(f"Synced {len(synced)} global commands")

    async def on_ready(self) -> None:
        print(f"We have logged in as {self.user}")


if __name__ == "__main__":
    Bot().run(os.environ["DISCORD_TOKEN"])
