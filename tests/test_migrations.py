import asyncio
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url

from bot.db.engine import create_engine
from tests import database
from tests.database import database_url, reset_database


def alembic_config(tmp_path: Path) -> Config:
    async def reset() -> None:
        await (await reset_database(tmp_path)).dispose()

    asyncio.run(reset())
    config = Config(toml_file="pyproject.toml")
    config.set_main_option("sqlalchemy.url", database_url(tmp_path))
    return config


def test_migrations_match_models(tmp_path: Path) -> None:
    config = alembic_config(tmp_path)
    command.upgrade(config, "head")
    # models 有改但沒寫 migration 時會失敗
    command.check(config)


def test_downgrade(tmp_path: Path) -> None:
    config = alembic_config(tmp_path)
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    # 降級之後可以再升級回來（downgrade 有確實刪掉所有東西）
    command.upgrade(config, "head")


def test_mysql_charset(tmp_path: Path) -> None:
    """測試用的 MySQL / MariaDB 預設字元集是 latin1（compose.yaml），資料表要自己指定 utf8mb4。"""
    url = database.url
    if url is None or make_url(url).get_backend_name() not in ("mysql", "mariadb"):
        pytest.skip("只有 MySQL / MariaDB")
    config = alembic_config(tmp_path)
    command.upgrade(config, "head")

    async def not_utf8mb4() -> list[tuple[str, str | None]]:
        engine = create_engine(url)
        async with engine.connect() as conn:
            tables = await conn.execute(
                text(
                    "SELECT TABLE_NAME, NULL FROM information_schema.TABLES"
                    " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_COLLATION NOT LIKE 'utf8mb4%'"
                )
            )
            # 之後的 migration 新增的欄位也要是 utf8mb4（沿用資料表的預設）
            columns = await conn.execute(
                text(
                    "SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMNS"
                    " WHERE TABLE_SCHEMA = DATABASE() AND CHARACTER_SET_NAME != 'utf8mb4'"
                )
            )
            rows = [(row[0], row[1]) for row in [*tables, *columns]]
        await engine.dispose()
        # alembic_version 是 Alembic 自己建立的，只存 migration 的版本
        return [row for row in rows if row[0] != "alembic_version"]

    assert asyncio.run(not_utf8mb4()) == []
