from fastapi import APIRouter

from bot.api.deps import AdminDep, DBDep, GuildDep
from bot.api.guild_utils import require_category_channel, require_text_channel
from bot.api.schemas import SettingsOut, SettingsPatch
from bot.db.queries import get_guild_settings

router = APIRouter(prefix="/settings")


@router.get("")
async def get_settings(_: AdminDep, db: DBDep) -> SettingsOut:
    settings = await get_guild_settings(db)
    await db.commit()
    return SettingsOut.model_validate(settings)


@router.patch("")
async def update_settings(
    body: SettingsPatch, _: AdminDep, guild: GuildDep, db: DBDep
) -> SettingsOut:
    if body.log_channel_id is not None:
        require_text_channel(guild, body.log_channel_id)
    if body.default_discord_category_id is not None:
        require_category_channel(guild, body.default_discord_category_id)

    settings = await get_guild_settings(db)
    for name, value in body.model_dump(exclude_unset=True).items():
        setattr(settings, name, value)
    await db.commit()
    return SettingsOut.model_validate(settings)
