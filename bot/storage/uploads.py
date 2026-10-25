"""網頁上傳的附件（docs/tickets.md 8.2、8.3）：瀏覽器用 presigned URL 直接上傳到 S3，不經過 bot。

不超過 PART_SIZE 的檔案用一個網址上傳；比較大的用 multipart upload，每一塊各自一個網址。
"""

from __future__ import annotations

import contextlib
import logging
import math
from datetime import timedelta
from typing import TYPE_CHECKING

from botocore.exceptions import ClientError
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.core.config import get_settings
from bot.db.models import AttachmentStatus, Source, TicketAttachment
from bot.db.queries import execute_update, get_guild_settings
from bot.db.types import utcnow
from bot.storage.files import MAGIC_BYTES, allowed_file_types, check_magic, detect_content_type
from bot.storage.s3 import PART_SIZE, is_not_found, object_exists
from bot.storage.status import finish

if TYPE_CHECKING:
    from types_aiobotocore_s3 import S3Client
    from types_aiobotocore_s3.type_defs import PartTypeDef

log = logging.getLogger(__name__)

# 上傳之後一直沒有送出的附件（例如關掉網頁）保留多久
UNSENT_RETENTION = timedelta(hours=24)


def is_multipart(size: int) -> bool:
    return size > PART_SIZE


def part_count(size: int) -> int:
    return math.ceil(size / PART_SIZE)


def part_size(size: int, number: int) -> int:
    # 每一塊都是 PART_SIZE，只有最後一塊比較小
    return min(PART_SIZE, size - PART_SIZE * (number - 1))


async def presign_put(s3: S3Client, attachment: TicketAttachment) -> str:
    assert attachment.storage_key is not None
    # 簽章包含 Content-Length（瀏覽器會自動帶上），大小不同時 S3 會拒絕
    return await s3.generate_presigned_url(
        "put_object",
        Params={
            "Bucket": get_settings().s3_bucket,
            "Key": attachment.storage_key,
            "ContentLength": attachment.size,
        },
        ExpiresIn=get_settings().s3_presign_ttl,
    )


async def start_multipart(s3: S3Client, attachment: TicketAttachment) -> str:
    assert attachment.storage_key is not None
    upload = await s3.create_multipart_upload(
        Bucket=get_settings().s3_bucket, Key=attachment.storage_key
    )
    return upload["UploadId"]


async def presign_part(s3: S3Client, attachment: TicketAttachment, number: int) -> str:
    assert attachment.storage_key is not None and attachment.upload_id is not None
    return await s3.generate_presigned_url(
        "upload_part",
        Params={
            "Bucket": get_settings().s3_bucket,
            "Key": attachment.storage_key,
            "UploadId": attachment.upload_id,
            "PartNumber": number,
            "ContentLength": part_size(attachment.size, number),
        },
        ExpiresIn=get_settings().s3_presign_ttl,
    )


async def finish_upload(s3: S3Client, attachment: TicketAttachment) -> bool:
    """確認瀏覽器已經上傳完；multipart upload 在這裡合併。還沒上傳完時回傳 False。"""
    assert attachment.storage_key is not None
    if attachment.upload_id is None:
        return await object_exists(s3, attachment.storage_key)
    bucket = get_settings().s3_bucket
    try:
        parts = await list_parts(s3, attachment)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "NoSuchUpload":
            raise
        # 同時送出了兩次，另一個請求已經合併完
        return await object_exists(s3, attachment.storage_key)
    expected = part_count(attachment.size)
    numbers = [part.get("PartNumber") for part in parts]
    if numbers != list(range(1, expected + 1)) or any(
        part.get("Size") != part_size(attachment.size, part.get("PartNumber", 0)) for part in parts
    ):
        return False
    # 每一塊的 ETag 由 bot 用 ListParts 取得，瀏覽器不需要讀取
    await s3.complete_multipart_upload(
        Bucket=bucket,
        Key=attachment.storage_key,
        UploadId=attachment.upload_id,
        MultipartUpload={
            "Parts": [
                {"ETag": part.get("ETag", ""), "PartNumber": part.get("PartNumber", 0)}
                for part in parts
            ]
        },
    )
    return True


async def list_parts(s3: S3Client, attachment: TicketAttachment) -> list[PartTypeDef]:
    assert attachment.storage_key is not None and attachment.upload_id is not None
    parts: list[PartTypeDef] = []
    marker = 0
    while True:
        page = await s3.list_parts(
            Bucket=get_settings().s3_bucket,
            Key=attachment.storage_key,
            UploadId=attachment.upload_id,
            PartNumberMarker=marker,
        )
        parts.extend(page.get("Parts", []))
        if not page.get("IsTruncated"):
            return sorted(parts, key=lambda part: part.get("PartNumber", 0))
        marker = page.get("NextPartNumberMarker", 0)


async def verify_attachment(db: AsyncSession, s3: S3Client, attachment_id: int) -> int | None:
    """檢查上傳完的檔案：實際大小與 magic bytes，不符合就刪除並標記為 rejected（8.3）。

    冪等：不是等待檢查的附件直接略過。回傳要接著放入 deliver_messages 的客服單 id
    （附件已經在訊息裡、訊息在等它檢查完時）。
    """
    attachment = await db.get(TicketAttachment, attachment_id)
    if (
        attachment is None
        or attachment.source is not Source.WEB
        or attachment.status is not AttachmentStatus.PENDING
        or attachment.uploaded_at is None
        or attachment.storage_key is None
    ):
        return None
    # requeue_pending_attachments 用它判斷工作有沒有被執行過
    attachment.attempts += 1
    await db.commit()
    settings = await get_guild_settings(db)
    bucket = get_settings().s3_bucket

    try:
        info = await s3.head_object(Bucket=bucket, Key=attachment.storage_key)
    except ClientError as error:
        if not is_not_found(error):
            raise
        log.warning("Attachment %d is missing from S3", attachment.id)
        await finish(db, attachment, AttachmentStatus.FAILED)
        return await waiting_ticket(db, attachment)
    size = info["ContentLength"]
    # presigned URL 的簽章已經包含大小，這裡再確認一次，不依賴 S3 服務的檢查
    if size != attachment.size or size > settings.max_attachment_size:
        log.info("Rejected attachment %d: %d bytes, not %d", attachment.id, size, attachment.size)
        await reject(db, s3, attachment)
        return await waiting_ticket(db, attachment)

    head = b""
    if size > 0:
        response = await s3.get_object(
            Bucket=bucket, Key=attachment.storage_key, Range=f"bytes=0-{MAGIC_BYTES - 1}"
        )
        async with response["Body"] as body:
            head = await body.read()
    detected = check_magic(head, attachment.filename, allowed_file_types(settings))
    if detected is not None:
        log.info("Rejected attachment %d: detected %s", attachment.id, detected)
        await reject(db, s3, attachment)
        return await waiting_ticket(db, attachment)

    attachment.content_type = detect_content_type(head)
    await finish(db, attachment, AttachmentStatus.DONE)
    return await waiting_ticket(db, attachment)


async def reject(db: AsyncSession, s3: S3Client, attachment: TicketAttachment) -> None:
    assert attachment.storage_key is not None
    await s3.delete_object(Bucket=get_settings().s3_bucket, Key=attachment.storage_key)
    attachment.storage_key = None
    await finish(db, attachment, AttachmentStatus.REJECTED)


async def give_up(db: AsyncSession, attachment_id: int) -> int | None:
    """最後一次檢查也失敗：標記為 failed，回傳值跟 verify_attachment 一樣。"""
    attachment = await db.get(TicketAttachment, attachment_id)
    if attachment is None or attachment.status is not AttachmentStatus.PENDING:
        return None
    await finish(db, attachment, AttachmentStatus.FAILED)
    return await waiting_ticket(db, attachment)


async def waiting_ticket(db: AsyncSession, attachment: TicketAttachment) -> int | None:
    # 狀態 commit 之後才重新讀取 message_id：同時送出的訊息如果比較早 commit，這裡會讀到；
    # 比較晚的話，送出訊息的 API 會放入 deliver_messages，那時已經讀得到這裡的狀態
    await db.refresh(attachment)
    return attachment.ticket_id if attachment.message_id is not None else None


async def discard(db: AsyncSession, s3: S3Client, attachment: TicketAttachment) -> bool:
    """刪除還沒送出的附件。已經被放進訊息的話不刪除，回傳 False。"""
    deleted = await execute_update(
        db,
        delete(TicketAttachment).where(
            TicketAttachment.id == attachment.id, TicketAttachment.message_id.is_(None)
        ),
    )
    await db.commit()
    if not deleted:
        return False
    # 先刪除資料再刪除檔案：刪除檔案失敗時只會留下用不到的檔案，不會有指向不存在檔案的附件
    bucket = get_settings().s3_bucket
    try:
        if attachment.upload_id is not None and attachment.uploaded_at is None:
            with contextlib.suppress(ClientError):
                await s3.abort_multipart_upload(
                    Bucket=bucket, Key=attachment.storage_key or "", UploadId=attachment.upload_id
                )
        if attachment.storage_key is not None:
            await s3.delete_object(Bucket=bucket, Key=attachment.storage_key)
    except ClientError:
        log.warning("Failed to delete the file of attachment %d", attachment.id, exc_info=True)
    return True


async def delete_unsent(db: AsyncSession, s3: S3Client, limit: int = 100) -> int:
    """刪除上傳後超過 UNSENT_RETENTION 還沒有送出的附件，回傳刪除了幾個。"""
    rows = list(
        await db.scalars(
            select(TicketAttachment)
            .where(
                TicketAttachment.source == Source.WEB,
                TicketAttachment.message_id.is_(None),
                TicketAttachment.created_at < utcnow() - UNSENT_RETENTION,
            )
            .order_by(TicketAttachment.id)
            .limit(limit)
        )
    )
    count = 0
    for attachment in rows:
        if await discard(db, s3, attachment):
            count += 1
    return count
