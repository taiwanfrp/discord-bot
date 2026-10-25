import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.sse import EventSourceResponse, ServerSentEvent
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.api.deps import (
    BotDep,
    GuildDep,
    HubDep,
    RedisDep,
    SessionmakerDep,
    SessionRedisDep,
    Viewer,
    authenticate,
    resolve_viewer,
)
from bot.api.schemas import EventOut
from bot.api.sse import LIST_EVENT_PREFIX, EventHub
from bot.db.models import Ticket, TicketEvent
from bot.tickets.actions import can_view

router = APIRouter()

# 每隔多久重新驗證一次 session 與權限（docs/auth.md 3.2）
REVALIDATE_INTERVAL = 60.0
# 重新連線時最多補送幾個事件
REPLAY_LIMIT = 1000

LastEventId = Annotated[int | None, Header(description="瀏覽器重新連線時自動帶上")]


def to_sse(event: TicketEvent) -> ServerSentEvent:
    data = EventOut.model_validate(event).model_dump(mode="json")
    return ServerSentEvent(id=str(event.id), event=event.type, data=data)


async def is_visible(db: AsyncSession, viewer: Viewer, event: TicketEvent) -> bool:
    ticket = await db.get(Ticket, event.ticket_id)
    return ticket is not None and await can_view(db, ticket, viewer.id, viewer.access)


async def event_stream(
    *,
    hub: EventHub,
    sessionmaker: async_sessionmaker[AsyncSession],
    ticket_id: int | None,
    last_event_id: int | None,
    check: Callable[[], Awaitable[Viewer]],
    interval: float = REVALIDATE_INTERVAL,
) -> AsyncIterator[ServerSentEvent]:
    """ticket_id 是 None 時是客服單列表：只送 ticket.* 事件，而且逐一檢查權限。

    check 重新驗證 session 與權限，失敗時丟出 HTTPException，連線就結束。
    """
    viewer = await check()
    # 先訂閱再補送，補送期間發生的事件才不會漏掉；同時推送進來的用補送過的 id 去除
    with hub.subscribe(ticket_id) as subscription:
        replayed: set[int] = set()
        if last_event_id is not None:
            query = (
                select(TicketEvent)
                .where(TicketEvent.id > last_event_id)
                .order_by(TicketEvent.id)
                .limit(REPLAY_LIMIT)
            )
            if ticket_id is not None:
                query = query.where(TicketEvent.ticket_id == ticket_id)
            else:
                query = query.where(TicketEvent.type.startswith(LIST_EVENT_PREFIX))
            async with sessionmaker() as db:
                for past in list(await db.scalars(query)):
                    replayed.add(past.id)
                    if ticket_id is not None or await is_visible(db, viewer, past):
                        yield to_sse(past)

        deadline = time.monotonic() + interval
        while True:
            try:
                event = await asyncio.wait_for(
                    subscription.queue.get(), timeout=max(deadline - time.monotonic(), 0)
                )
            except TimeoutError:
                try:
                    viewer = await check()
                except HTTPException:
                    return
                deadline = time.monotonic() + interval
                continue
            if event is None:
                return
            # 不能用「id 比上一個小就略過」：id 是新增時就決定的，兩個交易同時寫入時，
            # 比較小的 id 可能比較晚 commit、比較晚推送過來
            if event.id in replayed:
                continue
            if ticket_id is None:
                async with sessionmaker() as db:
                    if not await is_visible(db, viewer, event):
                        continue
            yield to_sse(event)


def checker(
    request: Request,
    redis: RedisDep,
    session_redis: SessionRedisDep,
    bot: BotDep,
    guild: GuildDep,
    sessionmaker: SessionmakerDep,
    ticket_id: int | None = None,
) -> Callable[[], Awaitable[Viewer]]:
    """驗證 session、取得使用者；指定客服單時也確認看得到它（看不到就是 404）。

    SSE 連線會維持很久，所以每次都用新的資料庫 session，不佔用連線。
    """

    async def check() -> Viewer:
        discord_id = await authenticate(request, redis, session_redis)
        async with sessionmaker() as db:
            viewer = await resolve_viewer(request, discord_id, bot, guild, db)
            if ticket_id is not None:
                ticket = await db.get(Ticket, ticket_id)
                if ticket is None or not await can_view(db, ticket, viewer.id, viewer.access):
                    raise HTTPException(404, "找不到這張客服單")
        return viewer

    return check


Checker = Annotated[Callable[[], Awaitable[Viewer]], Depends(checker)]


async def connect(check: Checker) -> Callable[[], Awaitable[Viewer]]:
    # 連線之前先驗證一次，才能回傳 401、404（開始串流之後就不能改狀態碼了）
    await check()
    return check


Connected = Annotated[Callable[[], Awaitable[Viewer]], Depends(connect)]


@router.get("/events", response_class=EventSourceResponse)
async def list_events(
    check: Connected,
    hub: HubDep,
    sessionmaker: SessionmakerDep,
    last_event_id: LastEventId = None,
) -> AsyncIterator[ServerSentEvent]:
    """客服單列表的變動（只有自己看得到的客服單）。"""
    async for item in event_stream(
        hub=hub,
        sessionmaker=sessionmaker,
        ticket_id=None,
        last_event_id=last_event_id,
        check=check,
    ):
        yield item


@router.get("/tickets/{ticket_id}/events", response_class=EventSourceResponse)
async def ticket_events(
    ticket_id: int,
    check: Connected,
    hub: HubDep,
    sessionmaker: SessionmakerDep,
    last_event_id: LastEventId = None,
) -> AsyncIterator[ServerSentEvent]:
    """單一客服單的所有事件（訊息、附件、接手、關閉等）。"""
    async for item in event_stream(
        hub=hub,
        sessionmaker=sessionmaker,
        ticket_id=ticket_id,
        last_event_id=last_event_id,
        check=check,
    ):
        yield item
