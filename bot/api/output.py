"""把資料庫的資料轉成 API 的回應（需要 S3 產生網址的部分）。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bot.api.schemas import AttachmentOut, UserOut
from bot.db.models import TicketAttachment, TicketUser
from bot.storage.share import avatar_url, download_url

if TYPE_CHECKING:
    from types_aiobotocore_s3 import S3Client


async def user_out(s3: S3Client, row: TicketUser) -> UserOut:
    out = UserOut.model_validate(row)
    out.avatar_url = await avatar_url(s3, row)
    return out


async def attachment_out(s3: S3Client, attachment: TicketAttachment) -> AttachmentOut:
    out = AttachmentOut.model_validate(attachment)
    out.download_url = await download_url(s3, attachment)
    return out
