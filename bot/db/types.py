from datetime import UTC, datetime
from functools import lru_cache

from cryptography.fernet import Fernet, MultiFernet
from sqlalchemy import DateTime, Dialect, Text
from sqlalchemy.dialects import mysql
from sqlalchemy.types import TypeDecorator, TypeEngine

from bot.core.config import get_settings


def utcnow() -> datetime:
    return datetime.now(UTC)


@lru_cache
def get_fernet() -> MultiFernet:
    keys = get_settings().encryption_keys.get_secret_value().split(",")
    return MultiFernet([Fernet(key.strip()) for key in keys if key.strip()])


class UTCDateTime(TypeDecorator[datetime]):
    """存成不含時區的 UTC 時間，讀出時加回 UTC 時區。"""

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[datetime]:
        # MySQL / MariaDB 的 DATETIME 預設只到秒
        if dialect.name in ("mysql", "mariadb"):
            return dialect.type_descriptor(mysql.DATETIME(fsp=6))
        return dialect.type_descriptor(DateTime())

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("datetime 必須帶有時區")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


class EncryptedText(TypeDecorator[str]):
    """寫入時用 MultiFernet 加密，讀出時解密。"""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return get_fernet().encrypt(value.encode()).decode()

    def process_result_value(self, value: str | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return get_fernet().decrypt(value).decode()
