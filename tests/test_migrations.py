from pathlib import Path

from alembic import command
from alembic.config import Config


def alembic_config(tmp_path: Path) -> Config:
    config = Config(toml_file="pyproject.toml")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
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
