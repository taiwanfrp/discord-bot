"""背景工作。每個工作都必須冪等，參數只傳 id（見 CLAUDE.md 的背景工作規則）。"""

import logging

import discord
from sqlalchemy import select

from bot.core.config import get_settings
from bot.core.redis import publish_event
from bot.db.engine import get_sessionmaker
from bot.db.models import Ticket, TicketStatus
from bot.tickets.open import add_opener, pending_openers_query
from bot.tickets.permissions import staff_role_ids, sync_role_overwrites
from bot.worker.broker import DiscordDep, broker

log = logging.getLogger(__name__)


@broker.task(schedule=[{"cron": "*/5 * * * *"}])
async def sync_pending_openers(discord_client: discord.Client = DiscordDep) -> None:
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


@broker.task
async def sync_ticket_permissions(
    discord_client: discord.Client = DiscordDep,
) -> None:
    """修改權限身份組之後，同步所有開啟中客服單頻道的身份組權限覆寫。"""
    async with get_sessionmaker()() as db:
        tickets = list(
            await db.scalars(
                select(Ticket).where(
                    Ticket.status == TicketStatus.OPEN, Ticket.channel_id.is_not(None)
                )
            )
        )
        if not tickets:
            return
        # 用 guild.fetch_channel 取得頻道，overwrites 才能對應到身份組
        guild = await discord_client.fetch_guild(get_settings().guild_id)
        for ticket in tickets:
            try:
                channel = await guild.fetch_channel(ticket.channel_id or 0)
            except discord.NotFound:
                continue
            if isinstance(channel, discord.TextChannel):
                role_ids = await staff_role_ids(db, ticket.category_id)
                await sync_role_overwrites(channel, guild, role_ids, ticket)
