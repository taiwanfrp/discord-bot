"""網頁上傳附件的流程（docs/tickets.md 8.2）。

API 先確認使用者看得到這張客服單；申請網址以外的操作，只有上傳的人自己可以做。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

import aiohttp
import discord
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Source, Ticket, TicketAttachment
from bot.db.queries import execute_update, get_guild_settings
from bot.db.types import utcnow
from bot.storage import uploads
from bot.storage.files import allowed_file_types, attachment_key, is_allowed_extension
from bot.storage.s3 import PART_SIZE
from bot.tickets.actions import require_open
from bot.tickets.errors import TicketError
from bot.tickets.snapshot import update_snapshot

if TYPE_CHECKING:
    from types_aiobotocore_s3 import S3Client

log = logging.getLogger(__name__)

# 同一個人在同一張客服單裡，最多同時有幾個還沒送出的附件
MAX_UNSENT = 10


@dataclass(frozen=True)
class Upload:
    attachment: TicketAttachment
    # 不超過 PART_SIZE：直接 PUT 到這個網址
    url: str | None = None
    # 超過：multipart upload，每一塊的網址用 sign_parts 取得
    part_size: int | None = None
    part_count: int | None = None


@contextmanager
def storage_errors() -> Iterator[None]:
    try:
        yield
    except (ClientError, BotoCoreError, aiohttp.ClientError) as error:
        log.exception("S3 request failed")
        raise TicketError("暫時無法連線到檔案儲存空間，請稍後再試", 502) from error


async def create_upload(
    db: AsyncSession,
    s3: S3Client,
    ticket: Ticket,
    uploader: discord.User | discord.Member,
    *,
    filename: str,
    size: int,
) -> Upload:
    """檢查副檔名與大小，建立附件並產生上傳網址。檔案內容上傳完之後由 worker 檢查。"""
    require_open(ticket)
    settings = await get_guild_settings(db)
    if not is_allowed_extension(filename, allowed_file_types(settings)):
        raise TicketError("不允許上傳這種類型的檔案", 422)
    if size > settings.max_attachment_size:
        raise TicketError(f"檔案超過大小上限（{settings.max_attachment_size:,} bytes）", 413)
    unsent = await db.scalar(
        select(func.count())
        .select_from(TicketAttachment)
        .where(
            TicketAttachment.ticket_id == ticket.id,
            TicketAttachment.uploader_id == uploader.id,
            TicketAttachment.message_id.is_(None),
        )
    )
    if (unsent or 0) >= MAX_UNSENT:
        raise TicketError("還沒送出的附件太多了，請先送出或移除", 409)

    attachment = TicketAttachment(
        ticket_id=ticket.id,
        uploader_id=uploader.id,
        source=Source.WEB,
        filename=filename,
        size=size,
    )
    db.add(attachment)
    await db.flush()
    attachment.storage_key = attachment_key(ticket.id, attachment.id, filename)
    await update_snapshot(db, ticket.id, uploader)
    with storage_errors():
        if uploads.is_multipart(size):
            attachment.upload_id = await uploads.start_multipart(s3, attachment)
            upload = Upload(attachment, part_size=PART_SIZE, part_count=uploads.part_count(size))
        else:
            upload = Upload(attachment, url=await uploads.presign_put(s3, attachment))
    await db.commit()
    return upload


async def sign_parts(
    s3: S3Client, attachment: TicketAttachment, numbers: list[int]
) -> list[tuple[int, str]]:
    """multipart upload 每一塊的上傳網址。網址有期限，所以前端在上傳之前才分批取得。"""
    if attachment.upload_id is None:
        raise TicketError("這個附件不是分塊上傳", 409)
    if attachment.uploaded_at is not None:
        raise TicketError("這個附件已經上傳完成", 409)
    count = uploads.part_count(attachment.size)
    if any(number < 1 or number > count for number in numbers):
        raise TicketError(f"part_number 要在 1 到 {count} 之間", 422)
    return [(number, await uploads.presign_part(s3, attachment, number)) for number in numbers]


async def complete_upload(db: AsyncSession, s3: S3Client, attachment: TicketAttachment) -> bool:
    """瀏覽器上傳完成。回傳 True 表示這次才完成，要放入 verify_attachment。"""
    if attachment.uploaded_at is not None:
        return False
    ticket = await db.get_one(Ticket, attachment.ticket_id)
    require_open(ticket)
    with storage_errors():
        done = await uploads.finish_upload(s3, attachment)
    if not done:
        raise TicketError("檔案還沒有上傳完成", 409)
    completed = await execute_update(
        db,
        update(TicketAttachment)
        .where(TicketAttachment.id == attachment.id, TicketAttachment.uploaded_at.is_(None))
        .values(uploaded_at=utcnow()),
    )
    await db.commit()
    await db.refresh(attachment)
    return completed > 0


async def discard_attachment(db: AsyncSession, s3: S3Client, attachment: TicketAttachment) -> None:
    """移除還沒送出的附件（使用者在送出前把它拿掉）。"""
    if attachment.message_id is not None or not await uploads.discard(db, s3, attachment):
        raise TicketError("已經送出的附件不能移除", 409)
