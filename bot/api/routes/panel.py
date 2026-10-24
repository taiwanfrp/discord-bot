import contextlib

import discord
from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from bot.api.deps import AdminDep, DBDep, GuildDep
from bot.api.guild_utils import discord_errors, require_text_channel
from bot.api.schemas import PanelOut, PanelPublishIn
from bot.db.models import TicketCategory
from bot.db.queries import get_guild_settings
from bot.tickets.panel import MAX_CATEGORIES, PanelView, panel_embed

router = APIRouter(prefix="/panel")


@router.post("/publish")
async def publish_panel(body: PanelPublishIn, _: AdminDep, guild: GuildDep, db: DBDep) -> PanelOut:
    """送出面板；同一個頻道已經有面板時，改成編輯它（選項跟著分類更新）。"""
    channel = require_text_channel(guild, body.channel_id)
    categories = list(
        await db.scalars(
            select(TicketCategory)
            .where(TicketCategory.enabled)
            .order_by(TicketCategory.position, TicketCategory.id)
        )
    )
    if len(categories) > MAX_CATEGORIES:
        raise HTTPException(422, f"啟用中的分類最多 {MAX_CATEGORIES} 個")

    settings = await get_guild_settings(db)
    embed, view = panel_embed(), PanelView(categories)
    message: discord.Message | None = None
    with discord_errors():
        if settings.panel_message_id is not None and settings.panel_channel_id == channel.id:
            try:
                message = await channel.get_partial_message(settings.panel_message_id).edit(
                    embed=embed, view=view
                )
            except discord.NotFound:
                # 面板被刪除了，重新送出
                message = None
        if message is None:
            message = await channel.send(embed=embed, view=view)
            await delete_old_panel(guild, settings.panel_channel_id, settings.panel_message_id)

    settings.panel_channel_id = channel.id
    settings.panel_message_id = message.id
    await db.commit()
    return PanelOut(channel_id=channel.id, message_id=message.id)


async def delete_old_panel(
    guild: discord.Guild, channel_id: int | None, message_id: int | None
) -> None:
    """換了頻道時，刪除舊頻道裡的面板（盡量刪除，失敗就算了）。"""
    if channel_id is None or message_id is None:
        return
    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        return
    with contextlib.suppress(discord.HTTPException):
        await channel.get_partial_message(message_id).delete()
