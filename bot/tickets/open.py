import contextlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime

import discord
from redis.asyncio import Redis
from redis.exceptions import LockError, RedisError
from sqlalchemy import Select, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import (
    GuildSettings,
    Source,
    Ticket,
    TicketCategory,
    TicketEvent,
    TicketStatus,
)
from bot.db.queries import execute_update, get_guild_settings
from bot.db.types import utcnow
from bot.storage.media import record_media
from bot.tickets.channel_name import render_channel_name
from bot.tickets.controls import TicketControlsView, claim_text
from bot.tickets.errors import TicketError
from bot.tickets.log import opened_log, send_log
from bot.tickets.permissions import (
    BOT_PERMISSIONS,
    EVERYONE_PERMISSIONS,
    MEMBER_PERMISSIONS,
    staff_role_ids,
)
from bot.tickets.snapshot import update_snapshot

log = logging.getLogger(__name__)

# 主要 API 封禁的帳號同時只能開 1 張客服單（docs/tickets.md 5.2）
BANNED_MAX_OPEN_TICKETS = 1


@dataclass(frozen=True)
class AccountBan:
    reason: str | None
    until: datetime | None  # None 是永久


# 開單訊息的 embed 只放內容的開頭，剩下的用一般訊息接在後面送出（docs/tickets.md 5.3）。
# Discord 對訊息的大小有沒公開的上限：主旨 100 字加內容 4000 字的中文就會被拒絕（413），
# 雖然都沒超過 embed 的字數限制
EMBED_CONTENT_LIMIT = 1000
# 一般訊息的字數上限
MESSAGE_LIMIT = 2000
CONTINUED = "\n…（接下一則訊息）"


async def open_ticket(
    db: AsyncSession,
    redis: Redis,
    guild: discord.Guild,
    opener: discord.User | discord.Member,
    *,
    category_id: int,
    subject: str,
    content: str,
    source: Source,
    session_redis: Redis,
) -> Ticket:
    """從 Discord 或網頁開單的共用流程（docs/tickets.md 5.3）。"""
    category = await db.get(TicketCategory, category_id)
    if category is None:
        raise TicketError("找不到這個分類", 404)
    if not category.enabled:
        raise TicketError("這個分類目前不開放開單")
    settings = await get_guild_settings(db)
    parent_id = category.discord_category_id or settings.default_discord_category_id
    parent = guild.get_channel(parent_id) if parent_id else None
    if not isinstance(parent, discord.CategoryChannel):
        raise TicketError("管理員還沒有設定客服單頻道要放在哪個 Discord 類別")

    # 開單者不在伺服器裡時，先不加進頻道（docs/tickets.md 5.2）
    member = opener if isinstance(opener, discord.Member) else guild.get_member(opener.id)
    ban = await get_account_ban(session_redis, opener.id)
    ticket = await create_ticket_row(
        db, redis, settings, member or opener, category, subject, content, source, ban
    )

    channel: discord.TextChannel | None = None
    try:
        channel = await guild.create_text_channel(
            render_channel_name(
                settings.channel_name_template,
                ticket_id=ticket.id,
                category=category.name,
                opener=opener.name,
            ),
            category=parent,
            overwrites=await build_overwrites(db, guild, category, member),
            reason=f"客服單 #{ticket.id}",
        )
        # 先存 channel_id，on_message 才找得到這張客服單
        ticket.channel_id = channel.id
        if member is not None:
            ticket.opener_added_at = utcnow()
        await db.commit()

        webhook = await channel.create_webhook(name="客服單", reason=f"客服單 #{ticket.id}")
        ticket.webhook_id = webhook.id
        ticket.webhook_token = webhook.token
        await db.commit()

        banned = member is None and await is_banned(guild, opener)
        head, rest = split_content(ticket.content)
        message = await channel.send(
            content=member.mention if member else None,
            embed=ticket_embed(
                ticket,
                category,
                opener,
                in_guild=member is not None,
                banned=banned,
                account_ban=ban,
                content=head,
            ),
            view=TicketControlsView(claimed=False),
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
        )
        for chunk in rest:
            # 使用者寫的內容：@everyone 等只顯示、不通知
            await channel.send(chunk, allowed_mentions=discord.AllowedMentions.none())
    except Exception as error:
        log.exception("Failed to set up ticket %d", ticket.id)
        if channel is not None:
            with contextlib.suppress(discord.HTTPException):
                await channel.delete(reason=f"客服單 #{ticket.id} 建立失敗")
        ticket.status = TicketStatus.FAILED
        await db.commit()
        raise TicketError("建立客服單頻道失敗，請稍後再試或聯絡管理員", 502) from error

    ticket.opening_message_id = message.id
    await db.commit()
    await record_media(db, f"{subject}\n{content}")
    await send_log(db, guild, ticket.id, opened_log(ticket, category.name))
    return ticket


async def create_ticket_row(
    db: AsyncSession,
    redis: Redis,
    settings: GuildSettings,
    opener: discord.User | discord.Member,
    category: TicketCategory,
    subject: str,
    content: str,
    source: Source,
    ban: AccountBan | None = None,
) -> Ticket:
    """檢查開單上限並建立資料。同一個人同時送出好幾次時，用鎖避免超過上限。"""
    lock = redis.lock(f"ticket-bot:lock:open:{opener.id}", timeout=30, blocking_timeout=10)
    try:
        acquired = await lock.acquire()
    except RedisError as error:
        raise TicketError("暫時無法開單，請稍後再試", 503) from error
    if not acquired:
        raise TicketError("開單的請求太多，請稍後再試", 429)
    try:
        open_count = await db.scalar(
            select(func.count())
            .select_from(Ticket)
            .where(Ticket.opener_id == opener.id, Ticket.status == TicketStatus.OPEN)
        )
        if ban is not None and (open_count or 0) >= BANNED_MAX_OPEN_TICKETS:
            raise TicketError(f"帳號封禁中，同時只能開 {BANNED_MAX_OPEN_TICKETS} 張客服單")
        if (open_count or 0) >= settings.max_open_tickets_per_user:
            raise TicketError(f"同時最多只能開 {settings.max_open_tickets_per_user} 張客服單")

        ticket = Ticket(
            category_id=category.id,
            source=source,
            opener_id=opener.id,
            subject=subject,
            content=content,
        )
        db.add(ticket)
        await db.flush()
        await update_snapshot(db, ticket.id, opener)
        db.add(
            TicketEvent(
                ticket_id=ticket.id,
                type="ticket.created",
                actor_id=opener.id,
                data={"source": source.value},
            )
        )
        await db.commit()
        return ticket
    finally:
        # 鎖過期（超過 timeout）時釋放會失敗，資料已經寫入，不影響結果
        with contextlib.suppress(LockError, RedisError):
            await lock.release()


async def build_overwrites(
    db: AsyncSession,
    guild: discord.Guild,
    category: TicketCategory,
    member: discord.Member | None,
) -> dict[discord.Role | discord.Member | discord.Object, discord.PermissionOverwrite]:
    overwrites: dict[
        discord.Role | discord.Member | discord.Object, discord.PermissionOverwrite
    ] = {
        guild.default_role: EVERYONE_PERMISSIONS,
        guild.me: BOT_PERMISSIONS,
    }
    for role_id in await staff_role_ids(db, category.id):
        role = guild.get_role(role_id)
        if role is not None:
            overwrites[role] = MEMBER_PERMISSIONS
    if member is not None:
        overwrites[member] = MEMBER_PERMISSIONS
    return overwrites


async def is_banned(guild: discord.Guild, user: discord.abc.Snowflake) -> bool:
    try:
        await guild.fetch_ban(user)
    except discord.NotFound:
        return False
    except discord.HTTPException:
        # 沒有查看停權名單的權限
        return False
    return True


async def get_account_ban(session_redis: Redis, user_id: int) -> AccountBan | None:
    """主要 API 封禁的帳號（docs/auth.md 5.5）；查不到、已經到期時是 None。"""
    try:
        raw = await session_redis.get(f"auth:ban:{user_id}")
    except RedisError:
        # 查不到時不擋開單，最多讓封禁的人多開幾張
        log.warning("Failed to read the account ban of %d", user_id, exc_info=True)
        return None
    if not isinstance(raw, str):
        return None
    try:
        data = json.loads(raw)
        until = datetime.fromisoformat(data["until"]) if data.get("until") else None
    except (json.JSONDecodeError, TypeError, KeyError, ValueError, AttributeError):
        log.warning("Invalid account ban of %d", user_id)
        return None
    if until is not None and until.tzinfo is None:
        return None
    if until is not None and until <= utcnow():
        return None
    reason = data.get("reason")
    return AccountBan(reason=reason if isinstance(reason, str) else None, until=until)


def split_content(content: str) -> tuple[str, list[str]]:
    """開單內容分成 embed 裡的開頭，以及接在後面的一般訊息。"""
    if len(content) <= EMBED_CONTENT_LIMIT:
        return content, []
    head, rest = cut(content, EMBED_CONTENT_LIMIT - len(CONTINUED))
    chunks: list[str] = []
    while rest:
        chunk, rest = cut(rest, MESSAGE_LIMIT)
        chunks.append(chunk)
    return head + CONTINUED, chunks


def cut(text: str, limit: int) -> tuple[str, str]:
    """切成不超過 limit 個字的前半段與剩下的部分，盡量在換行切開。"""
    if len(text) <= limit:
        return text, ""
    index = text.rfind("\n", limit // 2, limit)
    if index == -1:
        index = limit
    return text[:index], text[index:].lstrip("\n")


def ticket_embed(
    ticket: Ticket,
    category: TicketCategory,
    opener: discord.User | discord.Member,
    *,
    in_guild: bool,
    banned: bool,
    content: str,
    account_ban: AccountBan | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"#{ticket.id} {ticket.subject}"[:256],
        description=content,
        color=discord.Color.blurple(),
        timestamp=ticket.created_at,
    )
    embed.add_field(name="分類", value=category.name)
    embed.add_field(name="開單者", value=f"{opener.mention}（{opener.name}）")
    embed.add_field(name="來源", value="Discord" if ticket.source is Source.DISCORD else "網頁")
    embed.add_field(name="接手", value=claim_text(ticket.claimed_by_id))
    if account_ban is not None:
        until = discord.utils.format_dt(account_ban.until) if account_ban.until else "永久"
        reason = account_ban.reason or "未提供"
        embed.add_field(
            name="帳號封禁",
            value=f"開單者的 TaiwanFRP 帳號封禁中，到期：{until}\n原因：{reason}"[:1024],
            inline=False,
        )
    if not in_guild:
        note = "開單者目前不在伺服器內" + ("，而且已被停權" if banned else "")
        embed.add_field(
            name="注意",
            value=f"{note}。對話只能在網頁上進行；開單者加入伺服器後，會自動加進這個頻道。",
            inline=False,
        )
    return embed


async def add_opener(
    db: AsyncSession, ticket: Ticket, channel: discord.TextChannel, member: discord.Member
) -> int | None:
    """把加入伺服器的開單者加進頻道。回傳寫入的 ticket_event.id；已經加過的話回傳 None。

    on_member_join 和 sync_pending_openers 可能同時處理同一張單，所以用條件式的
    UPDATE 確保只有一邊會寫入事件、送出通知。
    """
    # 權限覆寫重複設定也沒關係
    await channel.set_permissions(
        member, overwrite=MEMBER_PERMISSIONS, reason=f"客服單 #{ticket.id} 的開單者加入伺服器"
    )
    updated = await execute_update(
        db,
        update(Ticket)
        .where(Ticket.id == ticket.id, Ticket.opener_added_at.is_(None))
        .values(opener_added_at=utcnow()),
    )
    if not updated:
        await db.rollback()
        return None
    await update_snapshot(db, ticket.id, member)
    event = TicketEvent(ticket_id=ticket.id, type="ticket.opener_added")
    db.add(event)
    await db.commit()
    await channel.send(
        f"{member.mention} 已加入伺服器，現在也可以在這個頻道對話。",
        allowed_mentions=discord.AllowedMentions(users=True),
    )
    return event.id


def pending_openers_query(opener_id: int | None = None) -> Select[Ticket]:
    """開啟中、開單者還沒加進頻道的客服單。"""
    query = select(Ticket).where(
        Ticket.status == TicketStatus.OPEN,
        Ticket.opener_added_at.is_(None),
        Ticket.channel_id.is_not(None),
    )
    if opener_id is not None:
        query = query.where(Ticket.opener_id == opener_id)
    return query
