import discord
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import PermissionRole, RoleKind, Ticket

# 權限覆寫（docs/tickets.md 6.3）
MEMBER_PERMISSIONS = discord.PermissionOverwrite(
    view_channel=True, send_messages=True, attach_files=True, read_message_history=True
)
BOT_PERMISSIONS = discord.PermissionOverwrite(
    view_channel=True,
    send_messages=True,
    embed_links=True,
    read_message_history=True,
    manage_channels=True,
    manage_permissions=True,
    manage_webhooks=True,
)


async def staff_role_ids(db: AsyncSession, category_id: int) -> set[int]:
    """這個分類的客服單頻道要開放的身份組：管理員身份組與分類身份組。"""
    rows = await db.scalars(
        select(PermissionRole.role_id).where(
            or_(PermissionRole.kind == RoleKind.ADMIN, PermissionRole.category_id == category_id)
        )
    )
    return set(rows)


async def sync_role_overwrites(
    channel: discord.TextChannel, guild: discord.Guild, role_ids: set[int], ticket: Ticket
) -> None:
    """讓頻道的身份組權限覆寫跟權限身份組的設定一致；成員的覆寫不動。

    channel 要是用 guild.fetch_channel 取得的，overwrites 才能分辨身份組與成員。
    """
    current: set[int] = set()
    reason = f"同步客服單 #{ticket.id} 的權限身份組"
    for target, overwrite in channel.overwrites.items():
        is_role = isinstance(target, discord.Role) or (
            isinstance(target, discord.Object) and target.type is discord.Role
        )
        # @everyone 的 id 等於伺服器的 id
        if not is_role or target.id == guild.id:
            continue
        role = guild.get_role(target.id)
        if target.id not in role_ids:
            if role is not None:
                await channel.set_permissions(role, overwrite=None, reason=reason)
        elif overwrite == MEMBER_PERMISSIONS:
            current.add(target.id)
    for role_id in role_ids - current:
        role = guild.get_role(role_id)
        if role is not None:
            await channel.set_permissions(role, overwrite=MEMBER_PERMISSIONS, reason=reason)
