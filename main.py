import os

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(
    command_prefix=commands.when_mentioned,
    help_command=None,
    intents=intents,
    allowed_contexts=app_commands.AppCommandContext(guild=True, dm_channel=True),
)


@bot.event
async def setup_hook() -> None:
    synced = await bot.tree.sync()
    print(f"Synced {len(synced)} global commands")


@bot.event
async def on_ready() -> None:
    print(f"We have logged in as {bot.user}")


@bot.tree.command(description="測試 bot 延遲")
async def ping(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(f"Pong! {round(bot.latency * 1000)}ms")


if __name__ == "__main__":
    bot.run(os.environ["DISCORD_TOKEN"])
