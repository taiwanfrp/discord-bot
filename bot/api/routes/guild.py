import discord
from fastapi import APIRouter
from sqlalchemy import delete

from bot.api.deps import AdminDep, DBDep, GuildDep
from bot.api.guild_utils import audit_reason, discord_errors, require_editable_role
from bot.api.schemas import ChannelOut, RoleIn, RoleOut, RolePatch
from bot.db.models import PermissionRole

router = APIRouter(prefix="/guild", tags=["Guild"])


def role_out(role: discord.Role) -> RoleOut:
    return RoleOut(
        id=role.id,
        name=role.name,
        color=role.color.value,
        position=role.position,
        hoist=role.hoist,
        mentionable=role.mentionable,
        managed=role.managed,
        assignable=role.is_assignable(),
    )


@router.get("/channels")
async def list_channels(_: AdminDep, guild: GuildDep) -> list[ChannelOut]:
    """伺服器的文字頻道與類別，需要管理員（設定紀錄頻道、分類要放在哪個類別時使用）。"""
    return [
        ChannelOut(
            id=channel.id,
            name=channel.name,
            type="category" if isinstance(channel, discord.CategoryChannel) else "text",
            parent_id=channel.category_id,
            position=channel.position,
        )
        for channel in guild.channels
        if isinstance(channel, discord.TextChannel | discord.CategoryChannel)
    ]


@router.get("/roles")
async def list_roles(_: AdminDep, guild: GuildDep) -> list[RoleOut]:
    """伺服器的身份組（不含 @everyone），需要管理員。"""
    return [role_out(role) for role in guild.roles if not role.is_default()]


@router.post("/roles", status_code=201)
async def create_role(body: RoleIn, viewer: AdminDep, guild: GuildDep) -> RoleOut:
    """在 Discord 建立身份組。用途另外用 PUT /permission-roles 設定。"""
    with discord_errors():
        role = await guild.create_role(
            name=body.name,
            colour=discord.Colour(body.color),
            hoist=body.hoist,
            mentionable=body.mentionable,
            reason=audit_reason(viewer),
        )
    return role_out(role)


@router.patch("/roles/{role_id}")
async def update_role(role_id: int, body: RolePatch, viewer: AdminDep, guild: GuildDep) -> RoleOut:
    """修改 Discord 的身份組，需要管理員；只能修改 bot 管得到的身份組。"""
    role = require_editable_role(guild, role_id)
    changes = body.model_dump(exclude_unset=True)
    if "color" in changes:
        changes["colour"] = discord.Colour(changes.pop("color"))
    with discord_errors():
        # 沒有任何修改時回傳 None
        edited = await role.edit(**changes, reason=audit_reason(viewer))
    return role_out(edited or role)


@router.delete("/roles/{role_id}", status_code=204)
async def delete_role(role_id: int, viewer: AdminDep, guild: GuildDep, db: DBDep) -> None:
    """刪除 Discord 的身份組，需要管理員；同時移除它的用途設定。"""
    role = require_editable_role(guild, role_id)
    with discord_errors():
        await role.delete(reason=audit_reason(viewer))
    await db.execute(delete(PermissionRole).where(PermissionRole.role_id == role_id))
    await db.commit()
