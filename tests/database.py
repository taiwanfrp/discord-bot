"""測試用的資料庫（uv run pytest --db mysql 等，見 tests/conftest.py）。

預設每個測試用一個新的 SQLite 檔案。選擇其他資料庫時用 compose.dev.yaml 的 db profile，
每個測試開始前把資料表全部刪掉再重建（測試不能同時執行）。
"""

from pathlib import Path

from sqlalchemy import Connection, MetaData
from sqlalchemy.ext.asyncio import AsyncEngine

from bot.db.base import Base
from bot.db.engine import create_engine

# compose.dev.yaml 的帳密
DATABASES = {
    "sqlite": None,
    "mysql": "mysql+aiomysql://ticket_bot:ticket-bot-dev-password@127.0.0.1:3306/ticket_bot_test?charset=utf8mb4",
    "mariadb": "mysql+aiomysql://ticket_bot:ticket-bot-dev-password@127.0.0.1:3307/ticket_bot_test?charset=utf8mb4",
    "postgres": "postgresql+asyncpg://ticket_bot:ticket-bot-dev-password@127.0.0.1:5432/ticket_bot_test",
}

# 由 tests/conftest.py 依照 --db 設定；None 表示 SQLite
url: str | None = None


def database_url(tmp_path: Path) -> str:
    return url or f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"


def _drop_all(conn: Connection) -> None:
    # 包括 models 裡沒有的資料表（例如 migration 測試留下的 alembic_version）
    existing = MetaData()
    existing.reflect(conn)
    existing.drop_all(conn)


async def reset_database(tmp_path: Path) -> AsyncEngine:
    """清空資料庫（SQLite 本來就是新的檔案），回傳 engine。"""
    engine = create_engine(database_url(tmp_path))
    if url is not None:
        async with engine.begin() as conn:
            await conn.run_sync(_drop_all)
    return engine


async def create_test_engine(tmp_path: Path) -> AsyncEngine:
    """建立跟 models 一樣的空資料表。"""
    engine = await reset_database(tmp_path)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine
