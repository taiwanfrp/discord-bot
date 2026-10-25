"""附件的分享連結與下載網址（docs/tickets.md 8.4、8.4.1）。"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import TYPE_CHECKING

from bot.core.config import get_settings
from bot.db.models import AttachmentStatus, TicketAttachment
from bot.storage.files import content_disposition, is_inline

if TYPE_CHECKING:
    from types_aiobotocore_s3 import S3Client

# 32 bytes 的隨機 token，base64url 之後的長度
TOKEN_LENGTH = 43


def new_share_token() -> tuple[str, str]:
    """回傳 token 與它的雜湊；資料庫只存雜湊。"""
    token = secrets.token_urlsafe(32)
    return token, hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def share_url(attachment_id: int, token: str) -> str:
    return f"{get_settings().public_base_url}/a/{attachment_id}/{token}"


def is_shared(attachment: TicketAttachment, token: str) -> bool:
    """token 正確、連結沒有被撤銷，而且檔案已經檢查完。"""
    if attachment.share_token_hash is None or attachment.share_revoked_at is not None:
        return False
    if attachment.status is not AttachmentStatus.DONE or attachment.storage_key is None:
        return False
    return hmac.compare_digest(hash_token(token), attachment.share_token_hash)


async def presign_download(s3: S3Client, attachment: TicketAttachment) -> str:
    """新的 presigned URL。Content-Type 與檔名由 S3 依照網址裡的參數回傳，不依賴物件本身的設定。"""
    assert attachment.storage_key is not None
    content_type = attachment.content_type or "application/octet-stream"
    return await s3.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": get_settings().s3_bucket,
            "Key": attachment.storage_key,
            "ResponseContentType": content_type,
            "ResponseContentDisposition": content_disposition(
                attachment.filename, inline=is_inline(content_type)
            ),
        },
        ExpiresIn=get_settings().s3_presign_ttl,
    )
