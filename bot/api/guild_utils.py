from collections.abc import Iterator
from contextlib import contextmanager

import discord
from fastapi import HTTPException

from bot.api.deps import Viewer


def require_text_channel(guild: discord.Guild, channel_id: int) -> discord.TextChannel:
    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        raise HTTPException(422, f"找不到文字頻道 {channel_id}")
    return channel


def require_category_channel(guild: discord.Guild, channel_id: int) -> discord.CategoryChannel:
    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.CategoryChannel):
        raise HTTPException(422, f"找不到 Discord 類別 {channel_id}")
    return channel


def require_role(guild: discord.Guild, role_id: int) -> discord.Role:
    role = guild.get_role(role_id)
    if role is None or role.is_default():
        raise HTTPException(422, f"找不到身份組 {role_id}")
    return role


def require_editable_role(guild: discord.Guild, role_id: int) -> discord.Role:
    role = require_role(guild, role_id)
    if not role.is_assignable():
        raise HTTPException(403, "bot 無法修改排在自己上面、或由整合服務管理的身份組")
    return role


def validate_emoji(guild: discord.Guild, emoji: str) -> None:
    # Unicode 表情無法事先檢查；自訂表情要在這個伺服器裡
    partial = discord.PartialEmoji.from_str(emoji)
    if partial.id is not None and guild.get_emoji(partial.id) is None:
        raise HTTPException(422, f"找不到自訂表情 {emoji}")


def audit_reason(viewer: Viewer) -> str:
    """Discord 稽核紀錄的原因。"""
    return f"{viewer.user}（{viewer.id}）從網頁操作"


@contextmanager
def discord_errors() -> Iterator[None]:
    """把呼叫 Discord 失敗的錯誤轉成 HTTP 錯誤。"""
    try:
        yield
    except discord.Forbidden as error:
        raise HTTPException(403, f"bot 沒有權限：{error.text}") from error
    except discord.HTTPException as error:
        raise HTTPException(502, f"Discord 回傳錯誤：{error.text}") from error
