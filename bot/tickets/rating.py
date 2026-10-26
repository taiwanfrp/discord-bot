"""關單私訊裡的按鈕：查看紀錄、評分（docs/tickets.md 第 10 節、10.1）。"""

import re
from typing import Any

import discord

from bot.core.config import get_settings
from bot.db.engine import get_sessionmaker
from bot.tickets import actions
from bot.tickets.controls import handle_error
from bot.tickets.errors import TicketError


def ticket_url(ticket_id: int) -> str:
    """網頁上的紀錄頁面。"""
    return get_settings().web_ticket_url.replace("{id}", str(ticket_id))


def closed_view(ticket_id: int, *, rating: bool) -> discord.ui.View:
    """rating=True 時多一個評分按鈕（只有開單者的私訊有）。"""
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="查看紀錄", url=ticket_url(ticket_id)))
    if rating:
        view.add_item(RateButton(ticket_id))
    return view


def parse_rating(value: str) -> int:
    """Modal 的分數是文字，只接受 1～5（全形數字也可以）。"""
    try:
        rating = int(value.strip())
    except ValueError:
        rating = 0
    if not actions.MIN_RATING <= rating <= actions.MAX_RATING:
        raise TicketError(
            f"分數只能是 {actions.MIN_RATING}～{actions.MAX_RATING}，請再按一次「評分」重新填寫",
            422,
        )
    return rating


def stars(rating: int) -> str:
    return "★" * rating + "☆" * (actions.MAX_RATING - rating) + f"（{rating} 分）"


async def show_rating(
    interaction: discord.Interaction, ticket_id: int, rating: int, feedback: str | None
) -> None:
    """把私訊的 embed 加上評分與評語，並拿掉評分按鈕（這則私訊只有開單者自己看得到）。"""
    assert interaction.message is not None
    embeds = [embed.copy() for embed in interaction.message.embeds]
    if embeds:
        embeds[0].description = None
        embeds[0].add_field(name="評分", value=stars(rating), inline=False)
        if feedback:
            embeds[0].add_field(name="評語", value=feedback[:1024], inline=False)
    await interaction.response.edit_message(
        embeds=embeds, view=closed_view(ticket_id, rating=False)
    )


class RateButton(
    discord.ui.DynamicItem[discord.ui.Button[discord.ui.View]],
    template=r"ticket:rate:(?P<id>[0-9]+)",
):
    """custom_id 帶客服單 id，bot 重啟後舊的私訊按鈕也還能用（Bot.setup_hook 註冊）。"""

    def __init__(self, ticket_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="評分",
                style=discord.ButtonStyle.primary,
                custom_id=f"ticket:rate:{ticket_id}",
            )
        )
        self.ticket_id = ticket_id

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Item[Any],
        match: re.Match[str],
    ) -> "RateButton":
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        # DynamicItem 的錯誤不會經過 View.on_error，自己處理
        try:
            async with get_sessionmaker()() as db:
                ticket = await actions.get_ticket(db, self.ticket_id)
            if (
                ticket.rating is not None
                and interaction.user.id == ticket.opener_id
                and interaction.message is not None
            ):
                # 已經在網頁評分過了：私訊改成顯示評分
                await show_rating(interaction, ticket.id, ticket.rating, ticket.feedback)
                await interaction.followup.send("已經評分過了", ephemeral=True)
                return
            actions.check_can_rate(ticket, interaction.user.id)
            await interaction.response.send_modal(RatingModal(self.ticket_id))
        except Exception as error:
            await handle_error(interaction, error)


class RatingModal(discord.ui.Modal, title="為這次的客服評分"):
    score: discord.ui.TextInput["RatingModal"] = discord.ui.TextInput(
        label="分數（1～5）", placeholder="5", min_length=1, max_length=1
    )
    feedback: discord.ui.TextInput["RatingModal"] = discord.ui.TextInput(
        label="評語（選填）", style=discord.TextStyle.paragraph, required=False, max_length=1000
    )

    def __init__(self, ticket_id: int) -> None:
        super().__init__()
        self.ticket_id = ticket_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        rating = parse_rating(self.score.value)
        async with get_sessionmaker()() as db:
            ticket = await actions.get_ticket(db, self.ticket_id)
            await actions.rate(db, ticket, interaction.user.id, rating, self.feedback.value)
        if interaction.message is not None:
            assert ticket.rating is not None
            await show_rating(interaction, ticket.id, ticket.rating, ticket.feedback)
            await interaction.followup.send("感謝你的評分！", ephemeral=True)
        else:
            await interaction.response.send_message("感謝你的評分！", ephemeral=True)

    # 跟 discord.py 的 Modal.on_error 簽名相同（它本身就和 View.on_error 不一致）
    async def on_error(  # type: ignore[override]
        self, interaction: discord.Interaction, error: Exception, /
    ) -> None:
        await handle_error(interaction, error)
