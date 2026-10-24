from collections.abc import Sequence

import discord

from bot.db.models import TicketCategory

PANEL_CUSTOM_ID = "ticket:panel"
# Discord 的下拉選單最多 25 個選項
MAX_CATEGORIES = 25


def panel_embed() -> discord.Embed:
    return discord.Embed(
        title="客服單",
        description="請在下方選擇分類，開啟客服單。",
        color=discord.Color.blurple(),
    )


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
        # TODO 第 5 步：跳出開單的 Modal
        await interaction.response.send_message("開單功能尚未開放。", ephemeral=True)


class PanelView(discord.ui.View):
    """面板訊息的下拉選單。是 persistent view，bot 重啟後還能使用。"""

    def __init__(self, categories: Sequence[TicketCategory] = ()) -> None:
        super().__init__(timeout=None)
        self.add_item(CategorySelect(categories))
