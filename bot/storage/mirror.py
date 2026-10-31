"""worker 把 Discord 的附件、頭像、表情與貼圖存到 S3（docs/tickets.md 8.1、8.6、8.7）。"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator
from itertools import groupby
from typing import TYPE_CHECKING

import aiohttp
import discord
from redis.exceptions import LockError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.core.config import get_settings
from bot.core.redis import get_redis
from bot.db.models import (
    AttachmentStatus,
    DiscordMedia,
    Ticket,
    TicketAttachment,
    TicketMessage,
    TicketUser,
)
from bot.db.queries import get_guild_settings
from bot.storage.files import (
    MAGIC_BYTES,
    allowed_file_types,
    attachment_key,
    avatar_key,
    check_magic,
)
from bot.storage.media import CONTENT_TYPES, discord_url, is_media, media_key, message_attachments
from bot.storage.s3 import object_exists, s3_client, upload_stream
from bot.storage.status import finish

if TYPE_CHECKING:
    from types_aiobotocore_s3 import S3Client

log = logging.getLogger(__name__)

CHUNK_SIZE = 1024 * 1024
# 頭像、表情、貼圖不會很大，超過就不存
MAX_SMALL_FILE_SIZE = 10 * 1024 * 1024
# mirror_attachment 的鎖要比 worker 的 idle_timeout（30 分鐘）長
ATTACHMENT_LOCK_TIMEOUT = 35 * 60
# 大檔案一邊下載一邊上傳，可能超過 aiohttp 預設的總時間（5 分鐘），所以只限制連線與每次讀取
DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=60)


class SourceGone(Exception):
    """Discord 上已經取不到這個檔案（訊息或頻道被刪除）。"""


class AttachmentBusy(Exception):
    """另一個 worker 正在處理這個附件，或是處理到一半當機了（鎖還沒過期）。"""


async def mirror_attachment(
    db: AsyncSession, discord_client: discord.Client, attachment_id: int
) -> None:
    """串流下載 Discord 的附件，檢查檔案類型後用 multipart upload 存到 S3。

    冪等：不是 pending 的附件直接略過。同一個附件同時只會有一個 worker 處理。
    """
    lock = get_redis().lock(
        f"ticket-bot:lock:attachment:{attachment_id}",
        timeout=ATTACHMENT_LOCK_TIMEOUT,
        blocking=False,
    )
    if not await lock.acquire():
        # 丟出例外讓 queue 稍後重試：到時候另一個 worker 已經處理完；如果它當機了，
        # 鎖過期之後由重試接手（直接結束的話，這個工作會被 ack，附件就一直是 pending）
        raise AttachmentBusy(attachment_id)
    try:
        attachment = await db.get(TicketAttachment, attachment_id)
        if attachment is None or attachment.status is not AttachmentStatus.PENDING:
            return
        # requeue_pending_attachments 用它判斷工作有沒有被執行過
        attachment.attempts += 1
        await db.commit()
        settings = await get_guild_settings(db)
        allowed = allowed_file_types(settings)
        key = attachment_key(attachment.ticket_id, attachment.id, attachment.filename)

        try:
            async with aiohttp.ClientSession(timeout=DOWNLOAD_TIMEOUT) as http:
                response = await open_source(http, discord_client, db, attachment)
                async with response:
                    chunks = response.content.iter_chunked(CHUNK_SIZE)
                    head = await read_head(chunks)
                    detected = check_magic(head, attachment.filename, allowed)
                    if detected is not None:
                        log.info("Rejected attachment %d: detected %s", attachment.id, detected)
                        await finish(db, attachment, AttachmentStatus.REJECTED)
                        return
                    async with s3_client() as s3:
                        size = await upload_stream(
                            s3,
                            key,
                            prepend(head, chunks),
                            content_type=attachment.content_type or "application/octet-stream",
                        )
        except SourceGone:
            await finish(db, attachment, AttachmentStatus.FAILED)
            return

        attachment.storage_key = key
        attachment.size = size
        await finish(db, attachment, AttachmentStatus.DONE)
    finally:
        # 鎖已經過期（超過 timeout）時釋放會失敗，結果已經寫入，不影響
        with contextlib.suppress(LockError):
            await lock.release()


async def open_source(
    http: aiohttp.ClientSession,
    discord_client: discord.Client,
    db: AsyncSession,
    attachment: TicketAttachment,
) -> aiohttp.ClientResponse:
    """先用存下來的網址；過期的話，重新取得 Discord 訊息拿新的網址。"""
    if attachment.source_url:
        response = await http.get(attachment.source_url)
        if response.ok:
            return response
        response.release()
        if response.status not in (403, 404):
            response.raise_for_status()

    url = await fresh_url(discord_client, db, attachment)
    response = await http.get(url)
    response.raise_for_status()
    return response


async def fresh_url(
    discord_client: discord.Client, db: AsyncSession, attachment: TicketAttachment
) -> str:
    ticket = await db.get(Ticket, attachment.ticket_id)
    message = await db.get(TicketMessage, attachment.message_id) if attachment.message_id else None
    if ticket is None or ticket.channel_id is None or message is None:
        raise SourceGone
    if message.discord_message_id is None:
        raise SourceGone
    try:
        channel = await discord_client.fetch_channel(ticket.channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise SourceGone
        discord_message = await channel.fetch_message(message.discord_message_id)
    except discord.NotFound as error:
        raise SourceGone from error
    # 轉發的訊息的附件在 message_snapshots 裡
    for item in message_attachments(discord_message):
        if item.id == attachment.discord_attachment_id:
            return item.url
    raise SourceGone


async def read_head(chunks: AsyncIterator[bytes]) -> bytes:
    """讀取檔案開頭，至少 MAGIC_BYTES 個 bytes（檔案比較小時就是整個檔案）。"""
    head = bytearray()
    async for chunk in chunks:
        head.extend(chunk)
        if len(head) >= MAGIC_BYTES:
            break
    return bytes(head)


async def prepend(head: bytes, chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    if head:
        yield head
    async for chunk in chunks:
        yield chunk


async def mirror_avatars(
    db: AsyncSession, http: aiohttp.ClientSession, s3: S3Client, limit: int = 100
) -> int:
    """把還沒存到 S3 的頭像存起來，回傳處理了幾個頭像。

    同一個人的同一個頭像（相同的 hash）只下載一次；S3 上已經有的話直接沿用。
    """
    rows = list(
        await db.scalars(
            select(TicketUser)
            .where(
                TicketUser.avatar_hash.is_not(None),
                TicketUser.avatar_key.is_(None),
                TicketUser.discord_avatar_url.is_not(None),
            )
            .order_by(TicketUser.user_id, TicketUser.avatar_hash)
            .limit(limit)
        )
    )
    if not rows:
        return 0

    count = 0
    for (user_id, avatar_hash), group in groupby(
        rows, key=lambda row: (row.user_id, row.avatar_hash)
    ):
        assert avatar_hash is not None
        url = next(iter(group)).discord_avatar_url or ""
        key = avatar_key(user_id, avatar_hash)
        if not await object_exists(s3, key):
            try:
                body = await download_small_file(http, url)
            except (aiohttp.ClientError, TimeoutError):
                # 暫時的錯誤（Discord 5xx、網路）：下次執行再試，不影響其他頭像
                log.warning("Failed to download the avatar of %d", user_id, exc_info=True)
                continue
            if body is None:
                # 之後顯示預設頭像
                await set_avatar(db, user_id, avatar_hash, key=None, url=None)
                continue
            await s3.put_object(
                Bucket=get_settings().s3_bucket,
                Key=key,
                Body=body,
                ContentType="image/gif" if key.endswith(".gif") else "image/webp",
            )
        await set_avatar(db, user_id, avatar_hash, key=key, url=url)
        count += 1
    return count


async def download_small_file(http: aiohttp.ClientSession, url: str) -> bytes | None:
    """檔案已經不存在（4xx）或超過 MAX_SMALL_FILE_SIZE 時回傳 None。"""
    async with http.get(url) as response:
        # 429 是暫時的；其他 4xx（403、404 等）表示這個檔案已經取不到了
        if 400 <= response.status < 500 and response.status != 429:
            return None
        response.raise_for_status()
        # 要讀到結束為止：content.read(n) 只會回傳目前已經收到的部分
        body = bytearray()
        async for chunk in response.content.iter_chunked(CHUNK_SIZE):
            body.extend(chunk)
            if len(body) > MAX_SMALL_FILE_SIZE:
                log.warning("File %s is too large", url)
                return None
    return bytes(body)


async def set_avatar(
    db: AsyncSession, user_id: int, avatar_hash: str, *, key: str | None, url: str | None
) -> None:
    """同一個人、同一個頭像的所有快照一起更新。"""
    await db.execute(
        update(TicketUser)
        .where(TicketUser.user_id == user_id, TicketUser.avatar_hash == avatar_hash)
        .values(avatar_key=key, discord_avatar_url=url)
    )
    await db.commit()


async def mirror_media(
    db: AsyncSession, http: aiohttp.ClientSession, s3: S3Client, limit: int = 100
) -> int:
    """把還沒存到 S3 的表情、貼圖存起來，回傳存了幾個。"""
    rows = list(
        await db.scalars(
            select(DiscordMedia)
            .where(DiscordMedia.status == AttachmentStatus.PENDING)
            .order_by(DiscordMedia.created_at)
            .limit(limit)
        )
    )
    count = 0
    for row in rows:
        key = media_key(row.kind, row.id, row.extension)
        if not await object_exists(s3, key):
            url = discord_url(row.kind, row.id, row.extension)
            try:
                body = await download_small_file(http, url)
            except (aiohttp.ClientError, TimeoutError):
                # 暫時的錯誤：下次執行再試，不影響其他的
                log.warning("Failed to download %s", url, exc_info=True)
                continue
            if body is None or not is_media(body, row.extension):
                # 已經取不到（例如表情被刪除），或內容跟副檔名不符：網頁之後還是轉址到 Discord
                row.status = AttachmentStatus.FAILED
                await db.commit()
                continue
            await s3.put_object(
                Bucket=get_settings().s3_bucket,
                Key=key,
                Body=body,
                ContentType=CONTENT_TYPES[row.extension],
            )
        row.storage_key = key
        row.status = AttachmentStatus.DONE
        await db.commit()
        count += 1
    return count
