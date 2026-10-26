import discord


async def get_text_channel(guild: discord.Guild, channel_id: int) -> discord.TextChannel | None:
    """bot 有頻道的快取；worker 只有 REST API，快取裡沒有時才向 Discord 取得。

    頻道不存在時回傳 None；沒有權限等其他錯誤照樣丟出（不能當作頻道已經被刪除）。
    """
    channel: discord.abc.GuildChannel | discord.Thread | None = guild.get_channel(channel_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(channel_id)
        except discord.NotFound:
            return None
    return channel if isinstance(channel, discord.TextChannel) else None
