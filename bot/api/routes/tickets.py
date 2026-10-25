from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

import discord
from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.api.deps import BotDep, DBDep, GuildDep, RedisDep, S3Dep, Viewer, ViewerDep
from bot.api.guild_utils import discord_errors
from bot.api.output import user_out
from bot.api.schemas import (
    CloseIn,
    MemberOut,
    RatingIn,
    RatingOut,
    TicketDetailOut,
    TicketIn,
    TicketOut,
    TicketSummaryOut,
    UserIdIn,
)
from bot.db.models import Source, Ticket, TicketMember, TicketStatus, TicketUser
from bot.tickets import actions
from bot.tickets.errors import TicketError
from bot.tickets.open import open_ticket

router = APIRouter(prefix="/tickets")


@contextmanager
def ticket_errors() -> Iterator[None]:
    """TicketError 與 Discord 的錯誤轉成 HTTP 錯誤。"""
    try:
        with discord_errors():
            yield
    except TicketError as error:
        raise HTTPException(error.status_code, error.message) from error


async def visible_ticket(db: AsyncSession, viewer: Viewer, ticket_id: int) -> Ticket:
    """沒有權限看的客服單也回傳 404，不透露它是否存在。"""
    with ticket_errors():
        ticket = await actions.get_ticket(db, ticket_id)
    if not await actions.can_view(db, ticket, viewer.id, viewer.access):
        raise HTTPException(404, "找不到這張客服單")
    return ticket


def require_member(viewer: Viewer) -> discord.Member:
    if viewer.member is None:
        raise HTTPException(403, "你不在伺服器裡，無法這麼做")
    return viewer.member


def require_target(guild: discord.Guild, user_id: int) -> discord.Member:
    member = guild.get_member(user_id)
    if member is None:
        raise HTTPException(422, "對象不在伺服器裡")
    return member


@router.get("")
async def list_tickets(
    viewer: ViewerDep,
    db: DBDep,
    s3: S3Dep,
    status: TicketStatus | None = None,
    category_id: int | None = None,
    opener_id: int | None = None,
    claimed_by_id: int | None = None,
    # false：還沒有人接手
    claimed: bool | None = None,
    # 用開單者的名稱搜尋（帳號名稱、顯示名稱、伺服器暱稱）
    q: Annotated[str | None, Query(min_length=1, max_length=32)] = None,
    before: int | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[TicketSummaryOut]:
    """看得到的客服單，新的在前面。下一頁用這一頁最後一筆的 id 當作 before。"""
    query = select(Ticket).where(actions.visible_filter(viewer.id, viewer.access))
    if status is not None:
        query = query.where(Ticket.status == status)
    if category_id is not None:
        query = query.where(Ticket.category_id == category_id)
    if opener_id is not None:
        query = query.where(Ticket.opener_id == opener_id)
    if claimed_by_id is not None:
        query = query.where(Ticket.claimed_by_id == claimed_by_id)
    if claimed is not None:
        query = query.where(
            Ticket.claimed_by_id.is_not(None) if claimed else Ticket.claimed_by_id.is_(None)
        )
    if q is not None:
        query = query.where(
            select(TicketUser.ticket_id)
            .where(
                TicketUser.ticket_id == Ticket.id,
                TicketUser.user_id == Ticket.opener_id,
                or_(
                    TicketUser.username.icontains(q, autoescape=True),
                    TicketUser.global_name.icontains(q, autoescape=True),
                    TicketUser.nickname.icontains(q, autoescape=True),
                ),
            )
            .exists()
        )
    if before is not None:
        query = query.where(Ticket.id < before)
    tickets = list(await db.scalars(query.order_by(Ticket.id.desc()).limit(limit)))

    rows = await db.scalars(
        select(TicketUser).where(TicketUser.ticket_id.in_([ticket.id for ticket in tickets]))
    )
    users = {(row.ticket_id, row.user_id): row for row in rows}
    result = []
    for ticket in tickets:
        out = TicketSummaryOut.model_validate(ticket)
        if opener := users.get((ticket.id, ticket.opener_id)):
            out.opener = await user_out(s3, opener)
        if ticket.claimed_by_id is not None and (
            claimer := users.get((ticket.id, ticket.claimed_by_id))
        ):
            out.claimed_by = await user_out(s3, claimer)
        result.append(out)
    return result


@router.get("/{ticket_id}")
async def get_ticket(ticket_id: int, viewer: ViewerDep, db: DBDep, s3: S3Dep) -> TicketDetailOut:
    """客服單的內容與紀錄頁面需要的資料（訊息用 GET /tickets/{id}/messages 分頁取得）。"""
    ticket = await visible_ticket(db, viewer, ticket_id)
    out = TicketDetailOut.model_validate(ticket)
    # 額外加入的成員看不到評分
    if viewer.id != ticket.opener_id and not viewer.access.is_staff(ticket.category_id):
        out.rating = out.feedback = out.rated_at = None
    rows = await db.scalars(
        select(TicketUser).where(TicketUser.ticket_id == ticket.id).order_by(TicketUser.user_id)
    )
    out.users = [await user_out(s3, row) for row in rows]
    members = await db.scalars(
        select(TicketMember)
        .where(TicketMember.ticket_id == ticket.id)
        .order_by(TicketMember.added_at)
    )
    out.members = [MemberOut.model_validate(member) for member in members]
    return out


@router.post("", status_code=201)
async def create_ticket(
    body: TicketIn, viewer: ViewerDep, guild: GuildDep, db: DBDep, redis: RedisDep
) -> TicketOut:
    """從網頁開單。開單者不在伺服器裡也可以（例如被停權的人申請解除停權）。"""
    with ticket_errors():
        ticket = await open_ticket(
            db,
            redis,
            guild,
            viewer.member or viewer.user,
            category_id=body.category_id,
            subject=body.subject,
            content=body.content,
            source=Source.WEB,
        )
    return TicketOut.model_validate(ticket)


@router.post("/{ticket_id}/claim")
async def claim(ticket_id: int, viewer: ViewerDep, guild: GuildDep, db: DBDep) -> TicketOut:
    ticket = await visible_ticket(db, viewer, ticket_id)
    with ticket_errors():
        await actions.claim(db, guild, ticket, require_member(viewer))
    return TicketOut.model_validate(ticket)


@router.post("/{ticket_id}/unclaim")
async def unclaim(ticket_id: int, viewer: ViewerDep, guild: GuildDep, db: DBDep) -> TicketOut:
    ticket = await visible_ticket(db, viewer, ticket_id)
    with ticket_errors():
        await actions.unclaim(db, guild, ticket, require_member(viewer))
    return TicketOut.model_validate(ticket)


@router.post("/{ticket_id}/transfer")
async def transfer(
    ticket_id: int, body: UserIdIn, viewer: ViewerDep, guild: GuildDep, db: DBDep
) -> TicketOut:
    ticket = await visible_ticket(db, viewer, ticket_id)
    target = require_target(guild, body.user_id)
    with ticket_errors():
        await actions.transfer(db, guild, ticket, require_member(viewer), target)
    return TicketOut.model_validate(ticket)


@router.post("/{ticket_id}/close")
async def close(
    ticket_id: int, body: CloseIn, viewer: ViewerDep, guild: GuildDep, db: DBDep
) -> TicketOut:
    ticket = await visible_ticket(db, viewer, ticket_id)
    with ticket_errors():
        await actions.close(db, guild, ticket, viewer.member or viewer.user, body.reason)
    return TicketOut.model_validate(ticket)


@router.post("/{ticket_id}/members", status_code=204)
async def add_member(
    ticket_id: int, body: UserIdIn, viewer: ViewerDep, guild: GuildDep, db: DBDep
) -> None:
    ticket = await visible_ticket(db, viewer, ticket_id)
    target = require_target(guild, body.user_id)
    with ticket_errors():
        await actions.add_member(db, guild, ticket, viewer.member or viewer.user, target)


@router.delete("/{ticket_id}/members/{user_id}", status_code=204)
async def remove_member(
    ticket_id: int, user_id: int, viewer: ViewerDep, bot: BotDep, guild: GuildDep, db: DBDep
) -> None:
    ticket = await visible_ticket(db, viewer, ticket_id)
    # 已經離開伺服器的人也要能移除
    with ticket_errors():
        target = guild.get_member(user_id) or await bot.fetch_user(user_id)
        await actions.remove_member(db, guild, ticket, viewer.member or viewer.user, target)


@router.put("/{ticket_id}/rating")
async def rate(ticket_id: int, body: RatingIn, viewer: ViewerDep, db: DBDep) -> RatingOut:
    """開單者在關單後評分，只能一次（跟 Discord 私訊裡的評分按鈕共用）。"""
    ticket = await visible_ticket(db, viewer, ticket_id)
    with ticket_errors():
        await actions.rate(db, ticket, viewer.id, body.rating, body.feedback)
    return RatingOut.model_validate(ticket)
