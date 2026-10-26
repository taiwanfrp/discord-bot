from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine as _create_async_engine

import bot.db.hooks  # noqa: F401  註冊 commit 之後推送 ticket_event 的 hook
from bot.core.config import get_settings


def create_engine(url: str, *, sqlite_foreign_keys: bool = True) -> AsyncEngine:
    parsed = make_url(url)
    if parsed.get_backend_name() in ("mysql", "mariadb"):
        # MySQL / MariaDB 預設是 REPEATABLE READ：交易裡讀過的資料之後不會更新，而且 MariaDB 11.8
        # 起（innodb_snapshot_isolation）更新被其他交易改過的資料會直接失敗，不會依照最新的資料
        # 判斷條件。改成跟 PostgreSQL 一樣的 READ COMMITTED，帶條件的 UPDATE 才能正確判斷
        return _create_async_engine(url, pool_pre_ping=True, isolation_level="READ COMMITTED")
    if parsed.get_backend_name() != "sqlite":
        return _create_async_engine(url, pool_pre_ping=True)

    # SQLite 不會自動建立資料夾
    if parsed.database and parsed.database != ":memory:":
        Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)

    engine = _create_async_engine(url)
    if sqlite_foreign_keys:
        # SQLite 預設不檢查外鍵，開啟才跟其他資料庫的行為一致
        @event.listens_for(engine.sync_engine, "connect")
        def _enable_foreign_keys(dbapi_connection: Any, _: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


@lru_cache
def get_engine() -> AsyncEngine:
    return create_engine(get_settings().database_url)


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)
