import logging

import discord
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Ticket
from bot.db.queries import get_guild_settings

log = logging.getLogger(__name__)


async def send_log(db: AsyncSession, guild: discord.Guild, embed: discord.Embed) -> None:
    """送到 log 頻道（有設定的話）。失敗只記錄，不影響客服單的操作。"""
    settings = await get_guild_settings(db)
    if settings.log_channel_id is None:
        return
    channel = guild.get_channel(settings.log_channel_id)
    if not isinstance(channel, discord.TextChannel):
        return
    try:
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        log.exception("Failed to send log message")


def mention(user_id: int | None, *, none: str = "（無）") -> str:
    return f"<@{user_id}>" if user_id is not None else none


def opened_log(ticket: Ticket, category_name: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"客服單 #{ticket.id} 已開啟",
        color=discord.Color.green(),
        timestamp=ticket.created_at,
    )
    embed.add_field(name="分類", value=category_name)
    embed.add_field(name="開單者", value=mention(ticket.opener_id))
    embed.add_field(name="頻道", value=f"<#{ticket.channel_id}>")
    return embed


def closed_log(ticket: Ticket, category_name: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"客服單 #{ticket.id} 已關閉",
        description=ticket.close_reason,
        color=discord.Color.red(),
        timestamp=ticket.closed_at,
    )
    embed.add_field(name="分類", value=category_name)
    embed.add_field(name="開單者", value=mention(ticket.opener_id))
    embed.add_field(name="接手", value=mention(ticket.claimed_by_id))
    embed.add_field(name="關閉者", value=mention(ticket.closed_by_id, none="系統"))
    return embed
