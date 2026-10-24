from collections.abc import Iterable
from dataclasses import dataclass

import discord
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import PermissionRole, RoleKind


@dataclass(frozen=True)
class Access:
    """使用者在客服單系統裡的權限（docs/tickets.md 第 4 節）。"""

    is_admin: bool
    # 有分類身份組的分類
    category_ids: frozenset[int]

    def is_staff(self, category_id: int) -> bool:
        return self.is_admin or category_id in self.category_ids


NO_ACCESS = Access(is_admin=False, category_ids=frozenset())


def build_access(rows: Iterable[PermissionRole], *, is_guild_admin: bool) -> Access:
    rows = list(rows)
    return Access(
        # 伺服器的管理員（Discord 的 Administrator 權限）也算管理員，
        # 否則還沒設定管理員身份組時，沒有人能修改設定
        is_admin=is_guild_admin or any(row.kind is RoleKind.ADMIN for row in rows),
        category_ids=frozenset(
            row.category_id
            for row in rows
            if row.kind is RoleKind.CATEGORY and row.category_id is not None
        ),
    )


async def get_access(session: AsyncSession, member: discord.Member | None) -> Access:
    # 不在伺服器裡的使用者沒有任何身份組，只能存取自己開的客服單
    if member is None:
        return NO_ACCESS
    role_ids = [role.id for role in member.roles]
    rows = await session.scalars(select(PermissionRole).where(PermissionRole.role_id.in_(role_ids)))
    return build_access(rows, is_guild_admin=member.guild_permissions.administrator)
