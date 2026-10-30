"""背景工作。每個工作都必須冪等，參數只傳 id（見 CLAUDE.md 的背景工作規則）。"""

import contextlib
import logging
from datetime import timedelta

import aiohttp
import discord
from redis.exceptions import LockError
from sqlalchemy import and_, or_, select
from taskiq import Context

from bot.core.config import get_settings
from bot.core.redis import get_redis
from bot.db.engine import get_sessionmaker
from bot.db.models import (
    AttachmentStatus,
    DeliveryStatus,
    Source,
    Ticket,
    TicketAttachment,
    TicketMessage,
    TicketStatus,
)
from bot.db.types import utcnow
from bot.storage import mirror, uploads
from bot.storage.s3 import s3_client
from bot.storage.status import finish
from bot.tickets import delivery, export, inactivity, notifications
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
                    await finish(db, attachment, AttachmentStatus.FAILED)
            raise


@broker.task
async def verify_attachment(attachment_id: int, context: Context = ContextDep) -> None:
    """檢查網頁上傳的檔案；附件已經在訊息裡的話，接著送出那則訊息。"""
    async with get_sessionmaker()() as db, s3_client() as s3:
        try:
            ticket_id = await uploads.verify_attachment(db, s3, attachment_id)
        except Exception:
            if is_last_attempt(context):
                await db.rollback()
                # 標記為 failed，等它的訊息就不用再等了（訊息照樣送出，只是沒有這個附件）
                ticket_id = await uploads.give_up(db, attachment_id)
                if ticket_id is not None:
                    await deliver_messages.kiq(ticket_id)
            raise
    if ticket_id is not None:
        await deliver_messages.kiq(ticket_id)


@broker.task
async def deliver_messages(
    ticket_id: int,
    discord_client: discord.Client = DiscordDep,
    context: Context = ContextDep,
) -> None:
    """依照順序把這張單的網頁訊息透過 webhook 送到 Discord。"""
    async with get_sessionmaker()() as db:
        await delivery.deliver_messages(
            db, get_redis(), discord_client, ticket_id, last_attempt=is_last_attempt(context)
        )


@broker.task
async def send_close_notifications(
    ticket_id: int,
    discord_client: discord.Client = DiscordDep,
    context: Context = ContextDep,
) -> None:
    """關單後私訊開單者（紀錄的連結與評分按鈕）與接手的人（紀錄的連結）。"""
    async with get_sessionmaker()() as db:
        await notifications.send_close_notifications(
            db, get_redis(), discord_client, ticket_id, last_attempt=is_last_attempt(context)
        )


@broker.task(schedule=[{"cron": "*/5 * * * *"}])
async def check_inactive_tickets(discord_client: discord.Client = DiscordDep) -> None:
    """閒置提醒與自動關閉（docs/tickets.md 第 9 節）。"""
    lock = get_redis().lock(
        "ticket-bot:lock:check_inactive_tickets", timeout=10 * 60, blocking=False
    )
    if not await lock.acquire():
        # 上一次還在執行；5 分鐘後會再執行，不需要重試
        return
    try:
        async with get_sessionmaker()() as db:
            await inactivity.check_inactive_tickets(db, discord_client)
    finally:
        with contextlib.suppress(LockError):
            await lock.release()


@broker.task(schedule=[{"cron": "*/10 * * * *"}])
async def requeue_close_notifications() -> None:
    """補放入 send_close_notifications（關單之後、放入 queue 之前中斷時）。

    只補一天內關閉的單：太久以前的私訊已經沒有意義。
    """
    now = utcnow()
    async with get_sessionmaker()() as db:
        ticket_ids = list(
            await db.scalars(
                select(Ticket.id).where(
                    Ticket.status == TicketStatus.CLOSED,
                    Ticket.close_notified_at.is_(None),
                    Ticket.closed_at < now - timedelta(minutes=10),
                    Ticket.closed_at > now - timedelta(days=1),
                )
            )
        )
    for ticket_id in ticket_ids:
        await send_close_notifications.kiq(ticket_id)


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


@broker.task(schedule=[{"cron": "* * * * *"}])
async def mirror_media() -> None:
    """把訊息裡還沒存到 S3 的表情、貼圖存起來（還沒存好之前，網頁先轉址到 Discord 的網址）。"""
    lock = get_redis().lock("ticket-bot:lock:mirror_media", timeout=10 * 60, blocking=False)
    if not await lock.acquire():
        # 上一次還在執行；下一分鐘會再執行，不需要重試
        return
    try:
        async with (
            get_sessionmaker()() as db,
            aiohttp.ClientSession() as http,
            s3_client() as s3,
        ):
            await mirror.mirror_media(db, http, s3)
    finally:
        with contextlib.suppress(LockError):
            await lock.release()


@broker.task(schedule=[{"cron": "*/10 * * * *"}])
async def requeue_pending_attachments() -> None:
    """補放入 mirror_attachment、verify_attachment。

    bot 存好附件之後、放入 queue 之前中斷的話，附件會一直是 pending。
    """
    before = utcnow() - timedelta(minutes=10)
    async with get_sessionmaker()() as db:
        rows = list(
            await db.execute(
                select(TicketAttachment.id, TicketAttachment.source).where(
                    TicketAttachment.status == AttachmentStatus.PENDING,
                    # 還沒有任何 worker 處理過（處理過的會由重試機制負責）
                    TicketAttachment.attempts == 0,
                    or_(
                        and_(
                            TicketAttachment.source == Source.DISCORD,
                            TicketAttachment.created_at < before,
                        ),
                        # 網頁上傳的附件在上傳完成之後才會檢查
                        and_(
                            TicketAttachment.source == Source.WEB,
                            TicketAttachment.uploaded_at < before,
                        ),
                    ),
                )
            )
        )
    for attachment_id, source in rows:
        if source is Source.DISCORD:
            await mirror_attachment.kiq(attachment_id)
        else:
            await verify_attachment.kiq(attachment_id)


@broker.task(schedule=[{"cron": "*/10 * * * *"}])
async def requeue_pending_messages() -> None:
    """補放入 deliver_messages（API 存好訊息之後、放入 queue 之前中斷時）。

    不看 attempts：送出的工作是以客服單為單位，多放一次也只會依照順序送出還沒送的訊息。
    """
    async with get_sessionmaker()() as db:
        ticket_ids = list(
            await db.scalars(
                select(TicketMessage.ticket_id)
                .where(
                    TicketMessage.source == Source.WEB,
                    TicketMessage.delivery_status == DeliveryStatus.PENDING,
                    TicketMessage.created_at < utcnow() - timedelta(minutes=10),
                )
                .distinct()
            )
        )
    for ticket_id in ticket_ids:
        await deliver_messages.kiq(ticket_id)


@broker.task(schedule=[{"cron": "0 * * * *"}])
async def delete_unsent_attachments() -> None:
    """刪除上傳之後一直沒有送出的附件（例如使用者關掉網頁）。"""
    async with get_sessionmaker()() as db, s3_client() as s3:
        await uploads.delete_unsent(db, s3)


@broker.task
async def export_ticket(export_id: int, context: Context = ContextDep) -> None:
    """產生客服單的匯出檔案，存到 S3（docs/tickets.md 8.8）。"""
    async with (
        get_sessionmaker()() as db,
        aiohttp.ClientSession() as http,
        s3_client() as s3,
    ):
        try:
            await export.export_ticket(
                db, s3, http, export_id, last_attempt=is_last_attempt(context)
            )
        except export.ExportBusy:
            # 結果由持有鎖的 worker 寫入
            raise
        except Exception:
            if is_last_attempt(context):
                await db.rollback()
                await export.give_up(db, export_id)
            raise


@broker.task(schedule=[{"cron": "30 * * * *"}])
async def delete_expired_exports() -> None:
    """刪除超過 24 小時的匯出檔案。"""
    async with get_sessionmaker()() as db, s3_client() as s3:
        await export.delete_expired(db, s3)
