"""建立 S3_BUCKET 並設定 CORS（本機開發用）：uv run python -m bot.storage.create_bucket

正式環境的 bucket 事先建好、設定好 CORS（設定的內容同下），bot 的金鑰只能存取那個 bucket，
不需要建立 bucket 的權限。
"""

import asyncio
import logging

from botocore.exceptions import ClientError

from bot.core.config import get_settings
from bot.storage.s3 import s3_client

log = logging.getLogger(__name__)


async def main() -> None:
    settings = get_settings()
    bucket = settings.s3_bucket
    async with s3_client() as client:
        try:
            await client.head_bucket(Bucket=bucket)
            log.info("Bucket %s already exists", bucket)
        except ClientError:
            await client.create_bucket(Bucket=bucket)
            log.info("Created bucket %s", bucket)
        # 瀏覽器直接用 presigned URL 上傳、下載（docs/tickets.md 8.4）
        await client.put_bucket_cors(
            Bucket=bucket,
            CORSConfiguration={
                "CORSRules": [
                    {
                        "AllowedOrigins": settings.api_cors_origins,
                        "AllowedMethods": ["GET", "HEAD", "PUT"],
                        "AllowedHeaders": ["*"],
                        # bot 用 ListParts 取得每一塊的 ETag；前端的上傳套件可能也會讀取
                        "ExposeHeaders": ["ETag"],
                        "MaxAgeSeconds": 3600,
                    }
                ]
            },
        )
        log.info("Allowed CORS from %s", ", ".join(settings.api_cors_origins))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    asyncio.run(main())
