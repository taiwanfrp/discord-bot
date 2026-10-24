import discord
from fastapi import APIRouter

from bot.api.deps import AdminDep, GuildDep
from bot.api.schemas import ChannelOut, RoleOut

router = APIRouter(prefix="/guild")


@router.get("/channels")
async def list_channels(_: AdminDep, guild: GuildDep) -> list[ChannelOut]:
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
    return [
        RoleOut(
            id=role.id,
            name=role.name,
            color=role.color.value,
            position=role.position,
            managed=role.managed,
            assignable=role.is_assignable(),
        )
        for role in guild.roles
        if not role.is_default()
    ]
