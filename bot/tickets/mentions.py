"""訊息裡提及的人、身份組與頻道（docs/tickets.md 8.6）。

人用客服單裡的快照；身份組與頻道是整個伺服器共用的，用伺服器現在的名稱。
"""

import re
from collections.abc import Iterable

import discord

USER_MENTION = re.compile(r"<@!?(\d{15,21})>")
ROLE_MENTION = re.compile(r"<@&(\d{15,21})>")
CHANNEL_MENTION = re.compile(r"<#(\d{15,21})>")

Channel = discord.abc.GuildChannel | discord.Thread


def mentioned_ids(pattern: re.Pattern[str], texts: Iterable[str]) -> set[int]:
    return {int(match[1]) for text in texts for match in pattern.finditer(text)}


def can_view(channel: Channel, viewer: discord.Member | discord.Role | None) -> bool:
    """跟 Discord 一樣，看不到的頻道不顯示名稱。viewer 是 None 表示客服（全部都看得到）。

    討論串依照它的上層頻道判斷；worker 沒有快取、找不到上層頻道時當作看不到。
    """
    if viewer is None:
        return True
    target = channel.parent if isinstance(channel, discord.Thread) else channel
    return target is not None and target.permissions_for(viewer).view_channel
