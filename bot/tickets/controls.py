import discord

CLOSE_CUSTOM_ID = "ticket:close"
CLOSE_WITH_REASON_CUSTOM_ID = "ticket:close_with_reason"
CLAIM_CUSTOM_ID = "ticket:claim"


class TicketControlsView(discord.ui.View):
    """客服單頻道裡的按鈕。是 persistent view，按下時用 channel_id 查出客服單。"""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    # TODO 第 6 步：關閉、附原因關閉、接手（接手後換成釋放）
    async def _not_ready(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message("這個功能尚未開放。", ephemeral=True)

    @discord.ui.button(label="關閉", style=discord.ButtonStyle.danger, custom_id=CLOSE_CUSTOM_ID)
    async def close(
        self, interaction: discord.Interaction, _: discord.ui.Button["TicketControlsView"]
    ) -> None:
        await self._not_ready(interaction)

    @discord.ui.button(
        label="附原因關閉",
        style=discord.ButtonStyle.secondary,
        custom_id=CLOSE_WITH_REASON_CUSTOM_ID,
    )
    async def close_with_reason(
        self, interaction: discord.Interaction, _: discord.ui.Button["TicketControlsView"]
    ) -> None:
        await self._not_ready(interaction)

    @discord.ui.button(label="接手", style=discord.ButtonStyle.success, custom_id=CLAIM_CUSTOM_ID)
    async def claim(
        self, interaction: discord.Interaction, _: discord.ui.Button["TicketControlsView"]
    ) -> None:
        await self._not_ready(interaction)
