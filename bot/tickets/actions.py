"""客服單的操作（docs/tickets.md 4.2、第 10 節、10.1）。

Discord 的按鈕、slash command 與 API 都呼叫這裡，權限也在這裡檢查。可能同時發生的操作
（例如兩個人同時接手）用帶條件的 UPDATE，確保只有一個成功。
"""

import logging
from collections.abc import Sequence
from typing import cast

import discord
from sqlalchemy import ColumnElement, or_, select, true, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from taskiq.exceptions import SendTaskError

from bot.core.permissions import Access, get_access
from bot.db.models import (
    Ticket,
    TicketAttachment,
    TicketCategory,
    TicketEvent,
    TicketMember,
    TicketStatus,
)
from bot.db.queries import execute_update
from bot.db.types import utcnow
from bot.tickets.channels import get_text_channel
from bot.tickets.controls import refresh_opening_message
from bot.tickets.errors import TicketError
from bot.tickets.log import closed_log, send_log
from bot.tickets.permissions import MEMBER_PERMISSIONS
from bot.tickets.snapshot import update_snapshot

log = logging.getLogger(__name__)

Actor = discord.User | discord.Member

MIN_RATING, MAX_RATING = 1, 5


async def get_ticket(db: AsyncSession, ticket_id: int) -> Ticket:
    ticket = await db.get(Ticket, ticket_id)
    if ticket is None:
        raise TicketError("找不到這張客服單", 404)
    return ticket


async def get_ticket_by_channel(db: AsyncSession, channel_id: int | None) -> Ticket:
    ticket = await db.scalar(select(Ticket).where(Ticket.channel_id == channel_id))
    if channel_id is None or ticket is None:
        raise TicketError("這裡不是客服單頻道", 404)
    return ticket


async def can_view(db: AsyncSession, ticket: Ticket, user_id: int, access: Access) -> bool:
    """開單者、這個分類的客服、額外加入的成員看得到這張客服單。"""
    if ticket.opener_id == user_id or access.is_staff(ticket.category_id):
        return True
    return await db.get(TicketMember, (ticket.id, user_id)) is not None


def visible_filter(user_id: int, access: Access) -> ColumnElement[bool]:
    """跟 can_view 相同的規則，用在客服單列表的查詢。"""
    if access.is_admin:
        return true()
    conditions = [
        Ticket.opener_id == user_id,
        Ticket.id.in_(select(TicketMember.ticket_id).where(TicketMember.user_id == user_id)),
    ]
    if access.category_ids:
        conditions.append(Ticket.category_id.in_(access.category_ids))
    return or_(*conditions)


async def actor_access(db: AsyncSession, actor: Actor) -> Access:
    return await get_access(db, actor if isinstance(actor, discord.Member) else None)


def require_open(ticket: Ticket) -> None:
    if ticket.status is not TicketStatus.OPEN:
        raise TicketError("這張客服單已經關閉")


async def require_manager(db: AsyncSession, ticket: Ticket, actor: Actor) -> Access:
    """開單者與這個分類的客服可以關閉、加入與移除成員；額外加入的成員不行。"""
    require_open(ticket)
    access = await actor_access(db, actor)
    if actor.id != ticket.opener_id and not access.is_staff(ticket.category_id):
        raise TicketError("只有開單者和這個分類的客服可以這麼做", 403)
    return access


async def check_can_close(db: AsyncSession, ticket: Ticket, actor: Actor) -> None:
    await require_manager(db, ticket, actor)


def ticket_channel(guild: discord.Guild, ticket: Ticket) -> discord.TextChannel | None:
    channel = guild.get_channel(ticket.channel_id) if ticket.channel_id else None
    return channel if isinstance(channel, discord.TextChannel) else None


async def announce(
    guild: discord.Guild, ticket: Ticket, content: str, *, ping: bool = False
) -> None:
    """在客服單頻道裡說明發生了什麼事。ping=False 時只顯示名稱，不通知對方。"""
    channel = ticket_channel(guild, ticket)
    if channel is None:
        return
    try:
        await channel.send(content, allowed_mentions=discord.AllowedMentions(users=ping))
    except discord.HTTPException:
        log.exception("Failed to announce in ticket %d", ticket.id)


def add_event(
    db: AsyncSession, ticket: Ticket, type: str, actor_id: int | None, **data: str | None
) -> None:
    # Discord ID 存成字串，跟 API 一樣
    db.add(TicketEvent(ticket_id=ticket.id, type=type, actor_id=actor_id, data=data or None))


async def claim(
    db: AsyncSession, guild: discord.Guild, ticket: Ticket, actor: discord.Member
) -> None:
    require_open(ticket)
    access = await get_access(db, actor)
    if not access.is_staff(ticket.category_id):
        raise TicketError("只有這個分類的客服可以接手", 403)
    claimed = await execute_update(
        db,
        update(Ticket)
        .where(
            Ticket.id == ticket.id,
            Ticket.status == TicketStatus.OPEN,
            Ticket.claimed_by_id.is_(None),
        )
        .values(claimed_by_id=actor.id, claimed_at=utcnow()),
    )
    if not claimed:
        await db.rollback()
        raise TicketError("這張客服單已經有人接手了，要換人請由接手的人或管理員轉交")
    await update_snapshot(db, ticket.id, actor)
    add_event(db, ticket, "ticket.claimed", actor.id)
    await db.commit()
    await db.refresh(ticket)

    await announce(guild, ticket, f"{actor.mention} 接手了這張客服單。")
    await refresh_opening_message(guild, ticket)


async def unclaim(
    db: AsyncSession, guild: discord.Guild, ticket: Ticket, actor: discord.Member
) -> None:
    require_open(ticket)
    previous = ticket.claimed_by_id
    if previous is None:
        raise TicketError("這張客服單還沒有人接手")
    access = await get_access(db, actor)
    if previous != actor.id and not access.is_admin:
        raise TicketError("只有目前接手的人和管理員可以釋放", 403)
    released = await execute_update(
        db,
        update(Ticket)
        .where(Ticket.id == ticket.id, Ticket.claimed_by_id == previous)
        .values(claimed_by_id=None, claimed_at=None),
    )
    if not released:
        await db.rollback()
        raise TicketError("接手的人已經變更，請重新整理")
    await update_snapshot(db, ticket.id, actor)
    add_event(db, ticket, "ticket.unclaimed", actor.id, previous_id=str(previous))
    await db.commit()
    await db.refresh(ticket)

    if previous == actor.id:
        await announce(guild, ticket, f"{actor.mention} 釋放了這張客服單。")
    else:
        await announce(guild, ticket, f"{actor.mention} 釋放了 <@{previous}> 接手的這張客服單。")
    await refresh_opening_message(guild, ticket)


async def transfer(
    db: AsyncSession,
    guild: discord.Guild,
    ticket: Ticket,
    actor: discord.Member,
    target: discord.Member,
) -> None:
    """轉交給同一個分類的客服或管理員（不能換分類）。管理員也可以直接指派還沒有人接手的單。"""
    require_open(ticket)
    previous = ticket.claimed_by_id
    access = await get_access(db, actor)
    if not access.is_admin and not (previous == actor.id and access.is_staff(ticket.category_id)):
        raise TicketError("只有目前接手的人和管理員可以轉交", 403)
    if target.id == previous:
        raise TicketError(f"{target.display_name} 已經是接手的人了")
    target_access = await get_access(db, target)
    if target.bot or not target_access.is_staff(ticket.category_id):
        raise TicketError("只能轉交給這個分類的客服或管理員", 422)
    transferred = await execute_update(
        db,
        update(Ticket)
        .where(
            Ticket.id == ticket.id,
            Ticket.status == TicketStatus.OPEN,
            Ticket.claimed_by_id.is_(None)
            if previous is None
            else Ticket.claimed_by_id == previous,
        )
        .values(claimed_by_id=target.id, claimed_at=utcnow()),
    )
    if not transferred:
        await db.rollback()
        raise TicketError("接手的人已經變更，請重新整理")
    await update_snapshot(db, ticket.id, actor)
    await update_snapshot(db, ticket.id, target)
    add_event(
        db,
        ticket,
        "ticket.transferred",
        actor.id,
        from_id=str(previous) if previous is not None else None,
        to_id=str(target.id),
    )
    await db.commit()
    await db.refresh(ticket)

    await announce(guild, ticket, f"{actor.mention} 把客服單轉交給 {target.mention}。", ping=True)
    await refresh_opening_message(guild, ticket)


async def add_member(
    db: AsyncSession, guild: discord.Guild, ticket: Ticket, actor: Actor, target: discord.Member
) -> None:
    await require_manager(db, ticket, actor)
    if target.bot:
        raise TicketError("不能把 bot 加進客服單", 422)
    if target.id == ticket.opener_id:
        raise TicketError("開單者本來就在客服單裡", 422)
    if (await get_access(db, target)).is_staff(ticket.category_id):
        raise TicketError("客服本來就看得到這張客服單，不需要另外加入", 422)
    if await db.get(TicketMember, (ticket.id, target.id)) is not None:
        raise TicketError(f"{target.display_name} 已經在客服單裡了")
    channel = ticket_channel(guild, ticket)
    if channel is None:
        raise TicketError("找不到客服單頻道")

    db.add(TicketMember(ticket_id=ticket.id, user_id=target.id, added_by_id=actor.id))
    await update_snapshot(db, ticket.id, actor)
    await update_snapshot(db, ticket.id, target)
    add_event(db, ticket, "ticket.member_added", actor.id, user_id=str(target.id))
    try:
        await db.flush()
    except IntegrityError as error:
        # 同時加入同一個人
        await db.rollback()
        raise TicketError(f"{target.display_name} 已經在客服單裡了") from error
    await channel.set_permissions(
        target, overwrite=MEMBER_PERMISSIONS, reason=f"加入客服單 #{ticket.id}"
    )
    await db.commit()

    await announce(guild, ticket, f"{actor.mention} 把 {target.mention} 加進了客服單。", ping=True)


async def remove_member(
    db: AsyncSession,
    guild: discord.Guild,
    ticket: Ticket,
    actor: Actor,
    target: discord.User | discord.Member,
) -> None:
    """target 可以是已經離開伺服器的使用者（discord.User）。"""
    await require_manager(db, ticket, actor)
    row = await db.get(TicketMember, (ticket.id, target.id))
    if row is None:
        raise TicketError(f"{target.display_name} 不是這張客服單額外加入的成員", 404)
    channel = ticket_channel(guild, ticket)
    if channel is None:
        raise TicketError("找不到客服單頻道")

    # 型別標註只寫了 Member，但 discord.py 也接受 User（已經離開伺服器的人）
    await channel.set_permissions(
        cast(discord.Member, target), overwrite=None, reason=f"移出客服單 #{ticket.id}"
    )
    await db.delete(row)
    await update_snapshot(db, ticket.id, actor)
    await update_snapshot(db, ticket.id, target)
    add_event(db, ticket, "ticket.member_removed", actor.id, user_id=str(target.id))
    await db.commit()

    await announce(guild, ticket, f"{actor.mention} 把 {target.mention} 移出了客服單。")


async def close(
    db: AsyncSession,
    guild: discord.Guild,
    ticket: Ticket,
    actor: Actor | None,
    reason: str | None,
    *,
    delete_channel: bool = True,
    conditions: Sequence[ColumnElement[bool]] = (),
) -> bool:
    """關單（docs/tickets.md 第 10 節）。actor 是 None 表示系統（自動關閉、頻道被刪除）。

    conditions 是關單當下還要成立的條件（例如自動關閉時，這段期間沒有新訊息）。
    回傳 False 表示已經關閉了，或是不符合 conditions（只有 actor 是 None 時；
    有 actor 時會丟出 TicketError）。
    """
    if actor is not None:
        await require_manager(db, ticket, actor)
    now = utcnow()
    closed = await execute_update(
        db,
        update(Ticket)
        .where(Ticket.id == ticket.id, Ticket.status == TicketStatus.OPEN, *conditions)
        .values(
            status=TicketStatus.CLOSED,
            closed_at=now,
            closed_by_id=actor.id if actor else None,
            close_reason=reason,
        ),
    )
    if not closed:
        await db.rollback()
        if actor is not None:
            raise TicketError("這張客服單已經關閉")
        return False
    # 頻道刪除後已經沒有人需要分享連結；就算被複製出去，關單後也打不開
    await db.execute(
        update(TicketAttachment)
        .where(TicketAttachment.ticket_id == ticket.id, TicketAttachment.share_revoked_at.is_(None))
        .values(share_revoked_at=now)
    )
    if actor is not None:
        # 關單後快照就不再更新，所以這是最後一次
        await update_snapshot(db, ticket.id, actor)
    add_event(db, ticket, "ticket.closed", actor.id if actor else None)
    await db.commit()
    await db.refresh(ticket)
    await queue_close_notifications(ticket.id)

    category = await db.get(TicketCategory, ticket.category_id)
    category_name = category.name if category else "（已刪除）"
    await send_log(db, guild, ticket.id, closed_log(ticket, category_name))
    if delete_channel and ticket.channel_id is not None:
        try:
            channel = await get_text_channel(guild, ticket.channel_id)
            if channel is not None:
                await channel.delete(reason=f"客服單 #{ticket.id} 已關閉")
        except discord.NotFound:
            pass
        except discord.HTTPException:
            log.exception("Failed to delete the channel of ticket %d", ticket.id)
    return True


async def queue_close_notifications(ticket_id: int) -> None:
    # 避免循環 import：worker 的工作也會關單（自動關閉）
    from bot.worker.tasks import send_close_notifications

    try:
        await send_close_notifications.kiq(ticket_id)
    except SendTaskError:
        # requeue_close_notifications 之後會補放
        log.exception("Failed to queue send_close_notifications for ticket %d", ticket_id)


def check_can_rate(ticket: Ticket, user_id: int) -> None:
    if user_id != ticket.opener_id:
        raise TicketError("只有開單者可以評分", 403)
    if ticket.status is not TicketStatus.CLOSED:
        raise TicketError("客服單關閉後才能評分")
    if ticket.rating is not None:
        raise TicketError("已經評分過了")


async def rate(
    db: AsyncSession, ticket: Ticket, user_id: int, rating: int, feedback: str | None
) -> None:
    """評分，每張客服單只能一次（Discord 的私訊與網頁共用）。"""
    check_can_rate(ticket, user_id)
    if not MIN_RATING <= rating <= MAX_RATING:
        raise TicketError(f"分數只能是 {MIN_RATING}～{MAX_RATING}", 422)
    rated = await execute_update(
        db,
        update(Ticket)
        .where(
            Ticket.id == ticket.id,
            Ticket.status == TicketStatus.CLOSED,
            Ticket.rating.is_(None),
        )
        .values(rating=rating, feedback=(feedback or "").strip() or None, rated_at=utcnow()),
    )
    if not rated:
        await db.rollback()
        raise TicketError("已經評分過了")
    # 客服單已經關閉，不再更新快照
    add_event(db, ticket, "ticket.rated", user_id)
    await db.commit()
    await db.refresh(ticket)
