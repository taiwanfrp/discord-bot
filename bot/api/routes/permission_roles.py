from fastapi import APIRouter, HTTPException
from sqlalchemy import delete, select

from bot.api.deps import AdminDep, DBDep, GuildDep
from bot.api.guild_utils import require_role
from bot.api.schemas import CategoryRoles, PermissionRoles
from bot.db.models import PermissionRole, RoleKind, TicketCategory

router = APIRouter(prefix="/permission-roles")


async def load(db: DBDep) -> PermissionRoles:
    rows = list(await db.scalars(select(PermissionRole).order_by(PermissionRole.id)))
    category_ids = await db.scalars(
        select(TicketCategory.id).order_by(TicketCategory.position, TicketCategory.id)
    )
    return PermissionRoles(
        admin_role_ids=[row.role_id for row in rows if row.kind is RoleKind.ADMIN],
        category_roles=[
            CategoryRoles(
                category_id=category_id,
                role_ids=[row.role_id for row in rows if row.category_id == category_id],
            )
            for category_id in category_ids
        ],
    )


@router.get("")
async def get_permission_roles(_: AdminDep, db: DBDep) -> PermissionRoles:
    return await load(db)


@router.put("")
async def replace_permission_roles(
    body: PermissionRoles, _: AdminDep, guild: GuildDep, db: DBDep
) -> PermissionRoles:
    category_ids = set(await db.scalars(select(TicketCategory.id)))
    rows: list[PermissionRole] = []
    # 用 dict 去除重複：category_id 是 null 時，資料庫的唯一限制不會生效
    for role_id in dict.fromkeys(body.admin_role_ids):
        require_role(guild, role_id)
        rows.append(PermissionRole(role_id=role_id, kind=RoleKind.ADMIN))
    for item in body.category_roles:
        if item.category_id not in category_ids:
            raise HTTPException(422, f"找不到分類 {item.category_id}")
        for role_id in dict.fromkeys(item.role_ids):
            require_role(guild, role_id)
            rows.append(
                PermissionRole(
                    role_id=role_id, kind=RoleKind.CATEGORY, category_id=item.category_id
                )
            )

    # 同一個 transaction 裡整份取代
    await db.execute(delete(PermissionRole))
    db.add_all(rows)
    await db.commit()
    return await load(db)
