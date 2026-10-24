from fastapi import APIRouter, HTTPException

from bot.api.deps import DBDep, GuildDep, RedisDep, ViewerDep
from bot.api.schemas import TicketIn, TicketOut
from bot.db.models import Source
from bot.tickets.open import TicketError, open_ticket

router = APIRouter(prefix="/tickets")


@router.post("", status_code=201)
async def create_ticket(
    body: TicketIn, viewer: ViewerDep, guild: GuildDep, db: DBDep, redis: RedisDep
) -> TicketOut:
    """從網頁開單。開單者不在伺服器裡也可以（例如被停權的人申請解除停權）。"""
    try:
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
    except TicketError as error:
        raise HTTPException(error.status_code, error.message) from error
    return TicketOut.model_validate(ticket)
