import os
from collections.abc import Iterator
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet

# 測試不使用 .env 裡的設定
os.environ["DISCORD_TOKEN"] = "test-token"
os.environ["DATABASE_URL"] = "sqlite+aiosqlite://"
# 測試一律用 fakeredis；指到沒有服務的 port，不小心連到真的 Redis 時會直接失敗
os.environ["REDIS_URL"] = "redis://127.0.0.1:1"
os.environ["SESSION_REDIS_URL"] = "redis://127.0.0.1:1"
os.environ["GUILD_ID"] = "1"
os.environ["API_CORS_ORIGINS"] = "https://web.test"
os.environ["ENCRYPTION_KEYS"] = Fernet.generate_key().decode()
# S3 一樣指到沒有服務的地方
os.environ["S3_ENDPOINT"] = "http://127.0.0.1:1"
os.environ["S3_ACCESS_KEY_ID"] = "test"
os.environ["S3_SECRET_ACCESS_KEY"] = "test"
os.environ["S3_BUCKET"] = "test"
os.environ["PUBLIC_BASE_URL"] = "https://bot.test"
os.environ["WEB_TICKET_URL"] = "https://web.test/tickets/{id}"


@pytest.fixture(autouse=True)
def published_events(monkeypatch: pytest.MonkeyPatch) -> Iterator[AsyncMock]:
    """commit 後推送 ticket_event 的 hook 不連 Redis；測試可以檢查推送了哪些事件。"""
    from bot.db import hooks

    publish = AsyncMock()
    monkeypatch.setattr(hooks, "publish_event", publish)
    yield publish


@pytest.fixture(autouse=True)
def close_notifications(monkeypatch: pytest.MonkeyPatch) -> Iterator[AsyncMock]:
    """關單時放入 queue 的 send_close_notifications（不真的放進 queue）。"""
    from bot.worker import tasks

    kiq = AsyncMock()
    monkeypatch.setattr(tasks.send_close_notifications, "kiq", kiq)
    yield kiq
