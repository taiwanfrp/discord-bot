from collections.abc import Iterator
from contextlib import contextmanager

import discord
from fastapi import APIRouter, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from bot.api.deps import BotDep, DBDep, GuildDep, RedisDep, Viewer, ViewerDep
from bot.api.guild_utils import discord_errors
from bot.api.schemas import CloseIn, TicketIn, TicketOut, UserIdIn
from bot.db.models import Source, Ticket
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
