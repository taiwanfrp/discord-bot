"""背景工作。每個工作都必須冪等，參數只傳 id（見 CLAUDE.md 的背景工作規則）。"""

import contextlib
import logging
from datetime import timedelta

import aiohttp
import discord
from redis.exceptions import LockError
from sqlalchemy import select
from taskiq import Context

from bot.core.config import get_settings
from bot.core.redis import get_redis
from bot.db.engine import get_sessionmaker
from bot.db.models import AttachmentStatus, Source, Ticket, TicketAttachment, TicketStatus
from bot.db.types import utcnow
from bot.storage import mirror
from bot.storage.s3 import s3_client
from bot.tickets.open import add_opener, pending_openers_query
from bot.tickets.permissions import staff_role_ids, sync_role_overwrites
from bot.worker.broker import ContextDep, DiscordDep, broker, is_last_attempt

log = logging.getLogger(__name__)


@broker.task(schedule=[{"cron": "*/5 * * * *"}])
async def sync_pending_openers(discord_client: discord.Client = DiscordDep) -> None:
    """把已經加入伺服器的開單者加進頻道，補 bot 離線時錯過的 on_member_join。"""
    async with get_sessionmaker()() as db:
        tickets = list(await db.scalars(pending_openers_query()))
        if not tickets:
            return
        guild = await discord_client.fetch_guild(get_settings().guild_id)
        for ticket in tickets:
            try:
                member = await guild.fetch_member(ticket.opener_id)
                channel = await discord_client.fetch_channel(ticket.channel_id or 0)
            except discord.NotFound:
                # 還沒加入伺服器，或頻道已經被刪除
                continue
            if not isinstance(channel, discord.TextChannel):
                continue
            # 寫入的 ticket_event 會在 commit 後自動通知 bot（bot/db/hooks.py）
            await add_opener(db, ticket, channel, member)


@broker.task
async def sync_ticket_permissions(
    discord_client: discord.Client = DiscordDep,
) -> None:
    """修改權限身份組之後，同步所有開啟中客服單頻道的身份組權限覆寫。"""
    async with get_sessionmaker()() as db:
        tickets = list(
            await db.scalars(
                select(Ticket).where(
                    Ticket.status == TicketStatus.OPEN, Ticket.channel_id.is_not(None)
                )
            )
        )
        if not tickets:
            return
        # 用 guild.fetch_channel 取得頻道，overwrites 才能對應到身份組
        guild = await discord_client.fetch_guild(get_settings().guild_id)
        for ticket in tickets:
            try:
                channel = await guild.fetch_channel(ticket.channel_id or 0)
            except discord.NotFound:
                continue
            if isinstance(channel, discord.TextChannel):
                role_ids = await staff_role_ids(db, ticket.category_id)
                await sync_role_overwrites(channel, guild, role_ids, ticket)


@broker.task
async def mirror_attachment(
    attachment_id: int,
    discord_client: discord.Client = DiscordDep,
    context: Context = ContextDep,
) -> None:
    """從 Discord 串流下載附件，檢查檔案類型後存到 S3。"""
    async with get_sessionmaker()() as db:
        try:
            await mirror.mirror_attachment(db, discord_client, attachment_id)
        except mirror.AttachmentBusy:
            # 結果由持有鎖的 worker 寫入，最後一次也不標記為 failed
            raise
        except Exception:
            if is_last_attempt(context):
                await db.rollback()
                attachment = await db.get(TicketAttachment, attachment_id)
                if attachment is not None and attachment.status is AttachmentStatus.PENDING:
                    await mirror.finish(db, attachment, AttachmentStatus.FAILED)
            raise


@broker.task(schedule=[{"cron": "* * * * *"}])
async def mirror_avatars() -> None:
    """把快照裡還沒存到 S3 的頭像存起來（還沒存好之前，網頁先用 Discord 的頭像網址）。"""
    lock = get_redis().lock("ticket-bot:lock:mirror_avatars", timeout=10 * 60, blocking=False)
    if not await lock.acquire():
        # 上一次還在執行；下一分鐘會再執行，不需要重試
        return
    try:
        async with (
            get_sessionmaker()() as db,
            aiohttp.ClientSession() as http,
            s3_client() as s3,
        ):
            await mirror.mirror_avatars(db, http, s3)
    finally:
        with contextlib.suppress(LockError):
            await lock.release()


@broker.task(schedule=[{"cron": "*/10 * * * *"}])
async def requeue_pending_attachments() -> None:
    """補放入 mirror_attachment。

    bot 存好附件之後、放入 queue 之前中斷的話，附件會一直是 pending。
    """
    async with get_sessionmaker()() as db:
        ids = list(
            await db.scalars(
                select(TicketAttachment.id).where(
                    TicketAttachment.source == Source.DISCORD,
                    TicketAttachment.status == AttachmentStatus.PENDING,
                    # 還沒有任何 worker 處理過（處理過的會由重試機制負責）
                    TicketAttachment.attempts == 0,
                    TicketAttachment.created_at < utcnow() - timedelta(minutes=10),
                )
            )
        )
    for attachment_id in ids:
        await mirror_attachment.kiq(attachment_id)
