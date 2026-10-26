from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any

from sqlalchemy import BigInteger, Enum, MetaData, Table
from sqlalchemy.orm import DeclarativeBase, mapped_column

from bot.db.types import UTCDateTime

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# Discord ID
Snowflake = Annotated[int, mapped_column(BigInteger)]


def _enum_values(enum: type[StrEnum]) -> list[str]:
    return [member.value for member in enum]


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map: dict[Any, Any] = {
        datetime: UTCDateTime(),
        StrEnum: Enum(StrEnum, native_enum=False, length=32, values_callable=_enum_values),
    }

    @classmethod
    def __table_cls__(cls, name: str, metadata: MetaData, *args: Any, **kwargs: Any) -> Table:
        # MySQL / MariaDB 的資料表一律使用 utf8mb4，不依賴資料庫的預設字元集（舊版是 latin1，
        # 存不進中文和 emoji）。排序規則用伺服器對 utf8mb4 的預設。其他資料庫會忽略這個設定。
        # 自動產生的 migration 也會帶上（op.create_table 的 mysql_charset）
        kwargs.setdefault("mysql_charset", "utf8mb4")
        return Table(name, metadata, *args, **kwargs)
