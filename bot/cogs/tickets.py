import logging
from collections.abc import Iterator
from contextlib import contextmanager

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select
from taskiq.exceptions import SendTaskError

from bot.core.cog import Cog
from bot.core.config import get_settings
from bot.core.tree import UserFacingError
from bot.db.engine import get_sessionmaker
from bot.db.models import Ticket, TicketStatus
from bot.tickets import actions
from bot.tickets.errors import TicketError
from bot.tickets.messages import save_discord_message
from bot.tickets.open import add_opener, pending_openers_query
from bot.worker.tasks import mirror_attachment

log = logging.getLogger(__name__)


@contextmanager
def user_errors() -> Iterator[None]:
    """TicketError 轉成 UserFacingError，由 CommandTree.on_error 回覆給使用者。"""
    try:
        yield
    except TicketError as error:
        raise UserFacingError(error.message) from error


def require_member(interaction: discord.Interaction) -> tuple[discord.Guild, discord.Member]:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        raise UserFacingError("這個指令只能在伺服器中使用")
    return interaction.guild, interaction.user


class Tickets(Cog):
    ticket = app_commands.Group(name="ticket", description="客服單", guild_only=True)

    @ticket.command(name="add", description="把成員加進這張客服單")
    @app_commands.describe(member="要加入的成員")
    async def add(self, interaction: discord.Interaction, member: discord.Member) -> None:
        guild, actor = require_member(interaction)
        await interaction.response.defer(ephemeral=True)
        async with get_sessionmaker()() as db:
            with user_errors():
                ticket = await actions.get_ticket_by_channel(db, interaction.channel_id)
                await actions.add_member(db, guild, ticket, actor, member)
        await interaction.followup.send(f"已把 {member.mention} 加進客服單。", ephemeral=True)

    @ticket.command(name="remove", description="把成員移出這張客服單")
    @app_commands.describe(member="要移出的成員（已經離開伺服器的人也可以）")
    async def remove(self, interaction: discord.Interaction, member: discord.User) -> None:
        guild, actor = require_member(interaction)
        await interaction.response.defer(ephemeral=True)
        async with get_sessionmaker()() as db:
            with user_errors():
                ticket = await actions.get_ticket_by_channel(db, interaction.channel_id)
                await actions.remove_member(db, guild, ticket, actor, member)
        await interaction.followup.send(f"已把 {member.mention} 移出客服單。", ephemeral=True)

    @ticket.command(name="transfer", description="把客服單轉交給這個分類的其他客服")
    @app_commands.describe(member="要轉交的客服")
    async def transfer(self, interaction: discord.Interaction, member: discord.Member) -> None:
        guild, actor = require_member(interaction)
        await interaction.response.defer(ephemeral=True)
        async with get_sessionmaker()() as db:
            with user_errors():
                ticket = await actions.get_ticket_by_channel(db, interaction.channel_id)
                await actions.transfer(db, guild, ticket, actor, member)
        await interaction.followup.send(f"已轉交給 {member.mention}。", ephemeral=True)

    @ticket.command(name="close", description="關閉這張客服單（頻道會被刪除）")
    @app_commands.describe(reason="關閉原因（選填）")
    async def close(
        self,
        interaction: discord.Interaction,
        reason: app_commands.Range[str, 1, 1000] | None = None,
    ) -> None:
        guild, actor = require_member(interaction)
        async with get_sessionmaker()() as db:
            with user_errors():
                ticket = await actions.get_ticket_by_channel(db, interaction.channel_id)
                await actions.check_can_close(db, ticket, actor)
            # 頻道刪除後就無法回覆，所以先回覆
            await interaction.response.send_message("正在關閉客服單……", ephemeral=True)
            with user_errors():
                await actions.close(db, guild, ticket, actor, reason)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """客服單頻道裡的訊息在送出的當下就加密存進資料庫（docs/tickets.md 8.1）。"""
        if message.guild is None or message.guild.id != get_settings().guild_id:
            return
        # bot 自己的訊息（開單的 embed、說明訊息）記錄在 ticket_event，不算對話內容
        if self.bot.user is not None and message.author.id == self.bot.user.id:
            return
        # 釘選、加入討論串等系統訊息
        if message.type not in (discord.MessageType.default, discord.MessageType.reply):
            return
        async with get_sessionmaker()() as db:
            ticket = await db.scalar(
                select(Ticket).where(
                    Ticket.channel_id == message.channel.id, Ticket.status == TicketStatus.OPEN
                )
            )
            if ticket is None:
                return
            # 從網頁送出的訊息（這張單自己的 webhook）已經存過了
            if message.webhook_id is not None and message.webhook_id == ticket.webhook_id:
                return
            attachment_ids = await save_discord_message(db, ticket, message)
        for attachment_id in attachment_ids:
            try:
                await mirror_attachment.kiq(attachment_id)
            except SendTaskError:
                # requeue_pending_attachments 之後會補放
                log.exception("Failed to queue mirror_attachment %d", attachment_id)

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

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        """客服單頻道在 Discord 上被直接刪除時，把客服單標記為關閉（關閉者是系統）。"""
        if channel.guild.id != get_settings().guild_id:
            return
        async with get_sessionmaker()() as db:
            ticket = await db.scalar(
                select(Ticket).where(
                    Ticket.channel_id == channel.id, Ticket.status == TicketStatus.OPEN
                )
            )
            # bot 自己關單時也會刪除頻道，那時客服單已經是關閉的，不會進來
            if ticket is not None:
                await actions.close(db, channel.guild, ticket, None, None, delete_channel=False)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Tickets(bot))
