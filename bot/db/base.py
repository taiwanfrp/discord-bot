from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any

from sqlalchemy import BigInteger, Enum, MetaData
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
