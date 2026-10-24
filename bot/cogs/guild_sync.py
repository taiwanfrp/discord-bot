import discord
from discord.ext import commands
from sqlalchemy import delete, update

from bot.core.cog import Cog
from bot.core.config import get_settings
from bot.db.engine import get_sessionmaker
from bot.db.models import PermissionRole, TicketCategory
from bot.db.queries import get_guild_settings


class GuildSync(Cog):
    """直接在 Discord 刪除身份組或頻道時，清掉資料庫裡指向它們的設定。"""

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        if role.guild.id != get_settings().guild_id:
            return
        async with get_sessionmaker()() as db:
            await db.execute(delete(PermissionRole).where(PermissionRole.role_id == role.id))
            await db.commit()

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        if channel.guild.id != get_settings().guild_id:
            return
        async with get_sessionmaker()() as db:
            settings = await get_guild_settings(db)
            if settings.log_channel_id == channel.id:
                settings.log_channel_id = None
            if settings.default_discord_category_id == channel.id:
                settings.default_discord_category_id = None
            if settings.panel_channel_id == channel.id:
                settings.panel_channel_id = None
                settings.panel_message_id = None
            await db.execute(
                update(TicketCategory)
                .where(TicketCategory.discord_category_id == channel.id)
                .values(discord_category_id=None)
            )
            await db.commit()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(GuildSync(bot))
