import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config

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
