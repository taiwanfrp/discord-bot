import logging

import discord
from discord.ext import commands

from bot.core.cog import Cog
from bot.core.config import get_settings
from bot.db.engine import get_sessionmaker
from bot.tickets.open import add_opener, pending_openers_query

log = logging.getLogger(__name__)


class Tickets(Cog):
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """開單者加入伺服器時，加進他所有開啟中客服單的頻道（docs/tickets.md 5.2）。"""
        if member.guild.id != get_settings().guild_id:
            return
        async with get_sessionmaker()() as db:
            tickets = list(await db.scalars(pending_openers_query(member.id)))
            for ticket in tickets:
                channel = member.guild.get_channel(ticket.channel_id or 0)
                if not isinstance(channel, discord.TextChannel):
                    continue
                try:
                    await add_opener(db, ticket, channel, member)
                except discord.HTTPException:
                    # sync_pending_openers 之後會再試一次
                    log.exception("Failed to add opener to ticket %d", ticket.id)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Tickets(bot))
