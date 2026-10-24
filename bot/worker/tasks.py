"""背景工作。每個工作都必須冪等，參數只傳 id（見 CLAUDE.md 的背景工作規則）。"""

import logging

import discord

from bot.core.config import get_settings
from bot.core.redis import publish_event
from bot.db.engine import get_sessionmaker
from bot.tickets.open import add_opener, pending_openers_query
from bot.worker.broker import DiscordClient, broker

log = logging.getLogger(__name__)


@broker.task(schedule=[{"cron": "*/5 * * * *"}])
async def sync_pending_openers(discord_client: DiscordClient) -> None:
    """把已經加入伺服器的開單者加進頻道，補 bot 離線時錯過的 on_member_join。"""
    async with get_sessionmaker()() as db:
        tickets = list(await db.scalars(pending_openers_query()))
        if not tickets:
            return
        guild = await discord_client.fetch_guild(get_settings().guild_id)
        for ticket in tickets:
            try:
                member = await guild.fetch_member(ticket.opener_id)
                channel = await discord_client.fetch_channel(ticket.channel_id or 0)
            except discord.NotFound:
                # 還沒加入伺服器，或頻道已經被刪除
                continue
            if not isinstance(channel, discord.TextChannel):
                continue
            event_id = await add_opener(db, ticket, channel, member)
            if event_id is not None:
                await publish_event(event_id)
