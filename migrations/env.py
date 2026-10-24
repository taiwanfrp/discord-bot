import asyncio
from logging.config import fileConfig
from typing import Any, Literal

from alembic import context
from alembic.autogenerate.api import AutogenContext
from sqlalchemy.engine import Connection
from sqlalchemy.types import TypeDecorator

import bot.db.models  # noqa: F401  讓所有 model 註冊到 metadata
from bot.core.config import get_settings
from bot.db.base import Base
from bot.db.engine import create_engine

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_url() -> str:
    # 測試時可以用 config.set_main_option("sqlalchemy.url", ...) 指定
    url = config.get_main_option("sqlalchemy.url")
    if url:
        return url
    return get_settings().database_url


def render_item(type_: str, obj: Any, autogen_context: AutogenContext) -> str | Literal[False]:
    # 自訂型別（UTCDateTime、EncryptedText）在 migration 裡需要 import
    if type_ == "type" and isinstance(obj, TypeDecorator):
        autogen_context.imports.add(f"import {type(obj).__module__}")
    return False


def configure(**kwargs: Any) -> None:
    context.configure(
        target_metadata=target_metadata,
        render_as_batch=True,
        render_item=render_item,
        **kwargs,
    )


def run_migrations_offline() -> None:
    configure(url=get_url(), literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    # SQLite 用 batch 模式重建資料表時，開著外鍵檢查會出錯
    engine = create_engine(get_url(), sqlite_foreign_keys=False)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
