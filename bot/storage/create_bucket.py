"""建立 S3_BUCKET（本機開發用）：uv run python -m bot.storage.create_bucket

正式環境的 bucket 事先建好，bot 的金鑰只能存取那個 bucket，不需要建立 bucket 的權限。
"""

import asyncio
import logging

from botocore.exceptions import ClientError

from bot.core.config import get_settings
from bot.storage.s3 import s3_client

log = logging.getLogger(__name__)


async def main() -> None:
    bucket = get_settings().s3_bucket
    async with s3_client() as client:
        try:
            await client.head_bucket(Bucket=bucket)
            log.info("Bucket %s already exists", bucket)
        except ClientError:
            await client.create_bucket(Bucket=bucket)
            log.info("Created bucket %s", bucket)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    asyncio.run(main())
