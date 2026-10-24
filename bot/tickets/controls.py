import logging
from typing import Any

import discord

from bot.db.engine import get_sessionmaker
from bot.db.models import Ticket
from bot.tickets.errors import TicketError

log = logging.getLogger(__name__)

CLOSE_CUSTOM_ID = "ticket:close"
CLOSE_WITH_REASON_CUSTOM_ID = "ticket:close_with_reason"
CLAIM_CUSTOM_ID = "ticket:claim"
UNCLAIM_CUSTOM_ID = "ticket:unclaim"

# 開單訊息的 embed 裡，顯示接手的人的欄位
CLAIM_FIELD = "接手"


def claim_text(claimed_by_id: int | None) -> str:
    return f"<@{claimed_by_id}>" if claimed_by_id is not None else "尚未接手"


async def send_error(interaction: discord.Interaction, message: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


async def handle_error(interaction: discord.Interaction, error: Exception) -> None:
    """按鈕與 Modal 的錯誤：TicketError 顯示給使用者，其他記錄下來。"""
    if isinstance(error, TicketError):
        await send_error(interaction, error.message)
        return
    log.error("Unhandled error in ticket interaction", exc_info=error)
    await send_error(interaction, "發生未預期的錯誤，請稍後再試")


async def refresh_opening_message(guild: discord.Guild, ticket: Ticket) -> None:
    """接手的人變更後，更新開單訊息的「接手」欄位與按鈕（接手 ↔ 釋放）。"""
    if ticket.channel_id is None or ticket.opening_message_id is None:
        return
    channel = guild.get_channel(ticket.channel_id)
    if not isinstance(channel, discord.TextChannel):
        return
    try:
        message = await channel.fetch_message(ticket.opening_message_id)
        embeds = message.embeds
        if embeds:
            set_claim_field(embeds[0], ticket.claimed_by_id)
        await message.edit(
            embeds=embeds, view=TicketControlsView(claimed=ticket.claimed_by_id is not None)
        )
    except discord.HTTPException:
        log.exception("Failed to update the opening message of ticket %d", ticket.id)


def set_claim_field(embed: discord.Embed, claimed_by_id: int | None) -> None:
    for index, field in enumerate(embed.fields):
        if field.name == CLAIM_FIELD:
            embed.set_field_at(
                index, name=CLAIM_FIELD, value=claim_text(claimed_by_id), inline=field.inline
            )
            return
    embed.add_field(name=CLAIM_FIELD, value=claim_text(claimed_by_id))


def require_member(interaction: discord.Interaction) -> tuple[discord.Guild, discord.Member]:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        raise TicketError("只能在伺服器裡使用")
    return interaction.guild, interaction.user


class TicketControlsView(discord.ui.View):
    """客服單頻道裡的按鈕。是 persistent view，按下時用 channel_id 查出客服單。

    claimed 決定顯示「接手」還是「釋放」；None 表示兩個都有，只能用來註冊 persistent view，
    不可以送出。
    """

    def __init__(self, *, claimed: bool | None) -> None:
        super().__init__(timeout=None)
        if claimed is True:
            self.remove_item(self.claim)
        elif claimed is False:
            self.remove_item(self.unclaim)

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item[Any]
    ) -> None:
        await handle_error(interaction, error)

    @discord.ui.button(label="關閉", style=discord.ButtonStyle.danger, custom_id=CLOSE_CUSTOM_ID)
    async def close(
        self, interaction: discord.Interaction, button: discord.ui.Button["TicketControlsView"]
    ) -> None:
        # 避免循環 import：actions 也會用到這個 view
        from bot.tickets import actions

        _, member = require_member(interaction)
        async with get_sessionmaker()() as db:
            ticket = await actions.get_ticket_by_channel(db, interaction.channel_id)
            await actions.check_can_close(db, ticket, member)
        await interaction.response.send_message(
            "確定要關閉這張客服單嗎？關閉後頻道會被刪除，紀錄可以在網頁上查看。",
            view=ConfirmCloseView(ticket.id),
            ephemeral=True,
        )

    @discord.ui.button(
        label="附原因關閉",
        style=discord.ButtonStyle.secondary,
        custom_id=CLOSE_WITH_REASON_CUSTOM_ID,
    )
    async def close_with_reason(
        self, interaction: discord.Interaction, button: discord.ui.Button["TicketControlsView"]
    ) -> None:
        from bot.tickets import actions

        _, member = require_member(interaction)
        async with get_sessionmaker()() as db:
            ticket = await actions.get_ticket_by_channel(db, interaction.channel_id)
            await actions.check_can_close(db, ticket, member)
        await interaction.response.send_modal(CloseReasonModal(ticket.id))

    @discord.ui.button(label="接手", style=discord.ButtonStyle.success, custom_id=CLAIM_CUSTOM_ID)
    async def claim(
        self, interaction: discord.Interaction, button: discord.ui.Button["TicketControlsView"]
    ) -> None:
        from bot.tickets import actions

        guild, member = require_member(interaction)
        await interaction.response.defer()
        async with get_sessionmaker()() as db:
            ticket = await actions.get_ticket_by_channel(db, interaction.channel_id)
            await actions.claim(db, guild, ticket, member)

    @discord.ui.button(
        label="釋放", style=discord.ButtonStyle.secondary, custom_id=UNCLAIM_CUSTOM_ID
    )
    async def unclaim(
        self, interaction: discord.Interaction, button: discord.ui.Button["TicketControlsView"]
    ) -> None:
        from bot.tickets import actions

        guild, member = require_member(interaction)
        await interaction.response.defer()
        async with get_sessionmaker()() as db:
            ticket = await actions.get_ticket_by_channel(db, interaction.channel_id)
            await actions.unclaim(db, guild, ticket, member)


async def close_from_interaction(
    interaction: discord.Interaction, ticket_id: int, reason: str | None
) -> None:
    from bot.tickets import actions

    guild, member = require_member(interaction)
    async with get_sessionmaker()() as db:
        ticket = await actions.get_ticket(db, ticket_id)
        await actions.close(db, guild, ticket, member, reason)


class ConfirmCloseView(discord.ui.View):
    def __init__(self, ticket_id: int) -> None:
        super().__init__(timeout=60)
        self.ticket_id = ticket_id

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item[Any]
    ) -> None:
        await handle_error(interaction, error)

    @discord.ui.button(label="確定關閉", style=discord.ButtonStyle.danger)
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button["ConfirmCloseView"]
    ) -> None:
        await interaction.response.edit_message(content="正在關閉客服單……", view=None)
        await close_from_interaction(interaction, self.ticket_id, None)

    @discord.ui.button(label="取消", style=discord.ButtonStyle.secondary)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button["ConfirmCloseView"]
    ) -> None:
        await interaction.response.edit_message(content="已取消。", view=None)


class CloseReasonModal(discord.ui.Modal, title="附原因關閉客服單"):
    reason: discord.ui.TextInput["CloseReasonModal"] = discord.ui.TextInput(
        label="關閉原因", style=discord.TextStyle.paragraph, max_length=1000
    )

    def __init__(self, ticket_id: int) -> None:
        super().__init__()
        self.ticket_id = ticket_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message("正在關閉客服單……", ephemeral=True)
        await close_from_interaction(interaction, self.ticket_id, self.reason.value)

    # 跟 discord.py 的 Modal.on_error 簽名相同（它本身就和 View.on_error 不一致）
    async def on_error(  # type: ignore[override]
        self, interaction: discord.Interaction, error: Exception, /
    ) -> None:
        await handle_error(interaction, error)
