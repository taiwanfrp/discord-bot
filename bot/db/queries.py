from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import GuildSettings


async def get_guild_settings(session: AsyncSession) -> GuildSettings:
    """取得全域設定；還沒有的話用預設值建立（id 固定為 1）。"""
    settings = await session.get(GuildSettings, 1)
    if settings is not None:
        return settings
    settings = GuildSettings(id=1)
    session.add(settings)
    try:
        await session.flush()
    except IntegrityError:
        # 另一個請求同時建立了
        await session.rollback()
        return await session.get_one(GuildSettings, 1)
    return settings
