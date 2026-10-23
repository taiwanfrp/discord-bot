import discord
from discord import app_commands
from discord.ext import commands

from bot.core.cog import Cog


class General(Cog):
    @app_commands.command(description="測試 bot 延遲")
    async def ping(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(f"Pong! {round(self.bot.latency * 1000)}ms")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(General(bot))
