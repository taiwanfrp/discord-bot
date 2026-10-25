from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from aiobotocore.config import AioConfig
from aiobotocore.session import get_session
from botocore.exceptions import ClientError

from bot.core.config import get_settings

if TYPE_CHECKING:
    # 只有型別資訊（開發用的套件），正式環境沒有安裝
    from types_aiobotocore_s3 import S3Client
    from types_aiobotocore_s3.type_defs import CompletedPartTypeDef

# multipart upload 每一塊的大小（S3 規定最後一塊以外至少 5 MiB）
PART_SIZE = 8 * 1024 * 1024


@asynccontextmanager
async def s3_client() -> AsyncIterator[S3Client]:
    """S3 相容服務的 client。只使用 S3 API，不依賴特定服務商的功能。"""
    settings = get_settings()
    async with get_session().create_client(
        "s3",
        endpoint_url=settings.s3_endpoint,
        region_name=settings.s3_region,
        aws_access_key_id=settings.s3_access_key_id,
        aws_secret_access_key=settings.s3_secret_access_key.get_secret_value(),
        # path-style（https://endpoint/bucket/key）所有 S3 相容服務都支援
        config=AioConfig(signature_version="s3v4", s3={"addressing_style": "path"}),
    ) as client:
        yield client


async def object_exists(client: S3Client, key: str) -> bool:
    try:
        await client.head_object(Bucket=get_settings().s3_bucket, Key=key)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return False
        raise
    return True


async def upload_stream(
    client: S3Client, key: str, chunks: AsyncIterator[bytes], *, content_type: str
) -> int:
    """一邊讀取一邊用 multipart upload 上傳，不會把整個檔案讀進記憶體。回傳檔案大小。"""
    bucket = get_settings().s3_bucket
    upload = await client.create_multipart_upload(Bucket=bucket, Key=key, ContentType=content_type)
    upload_id = upload["UploadId"]
    parts: list[CompletedPartTypeDef] = []
    size = 0
    buffer = bytearray()

    async def flush() -> None:
        number = len(parts) + 1
        result = await client.upload_part(
            Bucket=bucket, Key=key, UploadId=upload_id, PartNumber=number, Body=bytes(buffer)
        )
        parts.append({"ETag": result["ETag"], "PartNumber": number})
        buffer.clear()

    try:
        async for chunk in chunks:
            buffer.extend(chunk)
            size += len(chunk)
            if len(buffer) >= PART_SIZE:
                await flush()
        # 最後一塊（也可能是唯一的一塊，空檔案也要有一塊）
        if buffer or not parts:
            await flush()
        await client.complete_multipart_upload(
            Bucket=bucket,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={"Parts": parts},
        )
    except BaseException:
        await client.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
        raise
    return size
