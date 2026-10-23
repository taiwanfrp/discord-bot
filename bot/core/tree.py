import logging

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger(__name__)


class UserFacingError(app_commands.AppCommandError):
    """訊息會直接顯示給使用者的錯誤"""


def error_message(error: app_commands.AppCommandError) -> str | None:
    """把錯誤轉成給使用者看的訊息，回傳 None 表示是未預期的錯誤"""
    if isinstance(error, UserFacingError):
        return str(error)
    if isinstance(error, app_commands.CommandOnCooldown):
        return f"指令冷卻中，請 {error.retry_after:.0f} 秒後再試"
    if isinstance(error, app_commands.MissingPermissions):
        return f"你缺少權限：{', '.join(error.missing_permissions)}"
    if isinstance(error, app_commands.BotMissingPermissions):
        return f"Bot 缺少權限：{', '.join(error.missing_permissions)}"
    if isinstance(error, app_commands.NoPrivateMessage):
        return "這個指令只能在伺服器中使用"
    if isinstance(error, app_commands.CheckFailure):
        return "你沒有權限使用這個指令"
    if isinstance(error, app_commands.CommandInvokeError) and isinstance(
        error.original, discord.Forbidden
    ):
        return "Bot 權限不足，請確認 Bot 的權限與身份組順序"
    return None


class CommandTree(app_commands.CommandTree[commands.Bot]):
    async def on_error(
        self,
        interaction: discord.Interaction[commands.Bot],
        error: app_commands.AppCommandError,
        /,
    ) -> None:
        message = error_message(error)
        if message is None:
            command = interaction.command.qualified_name if interaction.command else "unknown"
            original = (
                error.original if isinstance(error, app_commands.CommandInvokeError) else error
            )
            log.error("Unhandled error in /%s", command, exc_info=original)
            message = "發生未預期的錯誤，請稍後再試"

        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            log.exception("Failed to send error message")
