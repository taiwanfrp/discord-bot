from fastapi import APIRouter, HTTPException
from sqlalchemy import exists, select

from bot.api.deps import AdminDep, DBDep, GuildDep, ViewerDep
from bot.api.guild_utils import require_category_channel, validate_emoji
from bot.api.schemas import CategoryIn, CategoryOut, CategoryPatch
from bot.db.models import Ticket, TicketCategory

router = APIRouter(prefix="/categories")


async def get_category(db: DBDep, category_id: int) -> TicketCategory:
    category = await db.get(TicketCategory, category_id)
    if category is None:
        raise HTTPException(404, "找不到這個分類")
    return category


def validate_category(body: CategoryIn | CategoryPatch, guild: GuildDep) -> None:
    if body.emoji is not None:
        validate_emoji(guild, body.emoji)
    if body.discord_category_id is not None:
        require_category_channel(guild, body.discord_category_id)


@router.get("")
async def list_categories(viewer: ViewerDep, db: DBDep) -> list[CategoryOut]:
    """所有登入的人都可以讀取（網頁開單時要選分類），只有管理員看得到停用的分類。"""
    query = select(TicketCategory).order_by(TicketCategory.position, TicketCategory.id)
    if not viewer.access.is_admin:
        query = query.where(TicketCategory.enabled)
    return [CategoryOut.model_validate(category) for category in await db.scalars(query)]


@router.post("", status_code=201)
async def create_category(body: CategoryIn, _: AdminDep, guild: GuildDep, db: DBDep) -> CategoryOut:
    validate_category(body, guild)
    category = TicketCategory(**body.model_dump())
    db.add(category)
    await db.commit()
    return CategoryOut.model_validate(category)


@router.patch("/{category_id}")
async def update_category(
    category_id: int, body: CategoryPatch, _: AdminDep, guild: GuildDep, db: DBDep
) -> CategoryOut:
    validate_category(body, guild)
    category = await get_category(db, category_id)
    for name, value in body.model_dump(exclude_unset=True).items():
        setattr(category, name, value)
    await db.commit()
    return CategoryOut.model_validate(category)


@router.delete("/{category_id}", status_code=204)
async def delete_category(category_id: int, _: AdminDep, db: DBDep) -> None:
    category = await get_category(db, category_id)
    # 客服單紀錄還要顯示分類，所以有客服單的分類只能停用
    if await db.scalar(select(exists().where(Ticket.category_id == category_id))):
        raise HTTPException(409, "這個分類已經有客服單，請改成停用")
    await db.delete(category)
    await db.commit()
