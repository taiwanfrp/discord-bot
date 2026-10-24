import contextlib
from collections.abc import Sequence

import discord
from sqlalchemy import select

from bot.core.redis import get_redis
from bot.db.engine import get_sessionmaker
from bot.db.models import Source, TicketCategory
from bot.tickets.open import TicketError, open_ticket

PANEL_CUSTOM_ID = "ticket:panel"
# Discord 的下拉選單最多 25 個選項
MAX_CATEGORIES = 25


def panel_embed() -> discord.Embed:
    return discord.Embed(
        title="客服單",
        description="請在下方選擇分類，開啟客服單。",
        color=discord.Color.blurple(),
    )


async def enabled_categories() -> list[TicketCategory]:
    async with get_sessionmaker()() as db:
        query = (
            select(TicketCategory)
            .where(TicketCategory.enabled)
            .order_by(TicketCategory.position, TicketCategory.id)
        )
        return list(await db.scalars(query))


class OpenTicketModal(discord.ui.Modal):
    subject: discord.ui.TextInput["OpenTicketModal"] = discord.ui.TextInput(
        label="主旨", max_length=100
    )
    content: discord.ui.TextInput["OpenTicketModal"] = discord.ui.TextInput(
        label="內容", style=discord.TextStyle.paragraph, max_length=4000
    )

    def __init__(self, category: TicketCategory) -> None:
        # Modal 的標題最多 45 個字
        super().__init__(title=f"開啟客服單：{category.name}"[:45])
        self.category_id = category.id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # 建立頻道、webhook 很可能超過 Discord 規定的 3 秒，先 defer
        await interaction.response.defer(ephemeral=True, thinking=True)
        if interaction.guild is None:
            return
        async with get_sessionmaker()() as db:
            try:
                ticket = await open_ticket(
                    db,
                    get_redis(),
                    interaction.guild,
                    interaction.user,
                    category_id=self.category_id,
                    subject=self.subject.value,
                    content=self.content.value,
                    source=Source.DISCORD,
                )
            except TicketError as error:
                await interaction.followup.send(error.message, ephemeral=True)
                return
        await interaction.followup.send(f"已建立客服單：<#{ticket.channel_id}>", ephemeral=True)


class CategorySelect(discord.ui.Select["PanelView"]):
    def __init__(self, categories: Sequence[TicketCategory]) -> None:
        if categories:
            super().__init__(
                custom_id=PANEL_CUSTOM_ID,
                placeholder="選擇分類",
                options=[
                    discord.SelectOption(
                        label=category.name,
                        value=str(category.id),
                        description=category.description,
                        emoji=category.emoji,
                    )
                    for category in categories
                ],
            )
        else:
            # 下拉選單至少要有一個選項，停用時也一樣
            super().__init__(
                custom_id=PANEL_CUSTOM_ID,
                placeholder="目前沒有可用的分類",
                options=[discord.SelectOption(label="目前沒有可用的分類", value="none")],
                disabled=True,
            )

    async def callback(self, interaction: discord.Interaction) -> None:
        categories = await enabled_categories()
        category = next((c for c in categories if str(c.id) == self.values[0]), None)
        if category is None:
            await interaction.response.send_message("這個分類目前不開放開單。", ephemeral=True)
        else:
            await interaction.response.send_modal(OpenTicketModal(category))

        # 重新送出下拉選單，讓它恢復成未選擇；選項用目前啟用中的分類
        if interaction.message is not None:
            with contextlib.suppress(discord.HTTPException):
                await interaction.message.edit(view=PanelView(categories))


class PanelView(discord.ui.View):
    """面板訊息的下拉選單。是 persistent view，bot 重啟後還能使用。"""

    def __init__(self, categories: Sequence[TicketCategory] = ()) -> None:
        super().__init__(timeout=None)
        self.add_item(CategorySelect(categories))
