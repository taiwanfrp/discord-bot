import re
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from fakeredis import FakeAsyncRedis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db.base import Base
from bot.db.engine import create_engine
from bot.db.models import (
    AttachmentStatus,
    DeliveryStatus,
    Source,
    Ticket,
    TicketAttachment,
    TicketCategory,
    TicketEvent,
    TicketMessage,
    TicketStatus,
    TicketUser,
)
from bot.storage.share import hash_token
from bot.tickets import delivery


class FakeWebhook:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        # 依序丟出的例外，用完之後就成功
        self.errors: list[Exception] = []
        self.on_send: Any = None

    async def send(self, content: str, **kwargs: Any) -> SimpleNamespace:
        if self.on_send is not None:
            await self.on_send(content)
        if self.errors:
            raise self.errors.pop(0)
        self.sent.append({"content": content, **kwargs})
        return SimpleNamespace(id=1000 + len(self.sent))


class Env:
    def __init__(self, db: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self.db = db
        self.sessionmaker = sessionmaker
        self.redis = FakeAsyncRedis()
        self.webhook = FakeWebhook()
        self.channel = MagicMock(spec=discord.TextChannel)
        self.channel.create_webhook = AsyncMock(return_value=SimpleNamespace(id=99, token="new"))
        self.client = MagicMock()
        self.client.fetch_channel = AsyncMock(return_value=self.channel)
        self.ticket = Ticket(
            category_id=0,
            source=Source.WEB,
            opener_id=5,
            subject="",
            content="",
            channel_id=900,
            webhook_id=7,
            webhook_token="token",
        )

    async def deliver(self, *, last_attempt: bool = False) -> None:
        await delivery.deliver_messages(
            self.db,
            self.redis,
            cast(discord.Client, self.client),
            self.ticket.id,
            last_attempt=last_attempt,
        )

    async def message(self, content: str = "你好") -> TicketMessage:
        message = TicketMessage(
            ticket_id=self.ticket.id,
            source=Source.WEB,
            author_id=5,
            content=content,
            delivery_status=DeliveryStatus.PENDING,
        )
        self.db.add(message)
        await self.db.commit()
        return message

    async def attach(
        self, message: TicketMessage, filename: str, status: AttachmentStatus
    ) -> TicketAttachment:
        attachment = TicketAttachment(
            ticket_id=self.ticket.id,
            message_id=message.id,
            uploader_id=5,
            source=Source.WEB,
            storage_key="k",
            filename=filename,
            size=1,
            status=status,
        )
        self.db.add(attachment)
        await self.db.commit()
        return attachment

    async def status(self, message: TicketMessage) -> DeliveryStatus | None:
        await self.db.refresh(message)
        return message.delivery_status


@pytest.fixture
async def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Env]:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as db:
        env = Env(db, sessionmaker)
        category = TicketCategory(name="帳務")
        db.add(category)
        await db.flush()
        env.ticket.category_id = category.id
        db.add(env.ticket)
        await db.flush()
        db.add(
            TicketUser(
                ticket_id=env.ticket.id,
                user_id=5,
                username="ming",
                global_name="小明",
                nickname="客服小明",
                discord_avatar_url="https://cdn.discordapp.com/avatars/5/h.webp",
            )
        )
        await db.commit()
        monkeypatch.setattr(delivery, "webhook_for", lambda *_: env.webhook)
        yield env
    await engine.dispose()


def http_error(status: int, message: str | dict[str, Any] = "") -> discord.HTTPException:
    response = MagicMock(status=status, reason="")
    if status == 404:
        return discord.NotFound(response, message)
    return discord.HTTPException(response, message)


async def test_delivers_in_order(env: Env) -> None:
    first = await env.message("第一則")
    image = await env.attach(first, "圖[1].png", AttachmentStatus.DONE)
    await env.attach(first, "a.exe", AttachmentStatus.REJECTED)
    second = await env.message("第二則")

    async def check_token(content: str) -> None:
        # Discord 收到訊息後馬上就會來抓預覽，送出時 token 的雜湊已經 commit 了
        match = re.search(r"/a/(\d+)/([\w-]+)\)", content)
        if match is None:
            return
        async with env.sessionmaker() as other:
            stored = await other.get_one(TicketAttachment, int(match[1]))
            assert stored.share_token_hash == hash_token(match[2])

    env.webhook.on_send = check_token
    await env.deliver()

    sent = env.webhook.sent
    assert [item["content"].split("\n")[0] for item in sent] == ["第一則", "第二則"]
    lines = sent[0]["content"].split("\n")
    # 方括號會讓連結的文字提早結束，換成圓括號
    link = rf"📎 \[圖\(1\)\.png\]\(https://bot\.test/a/{image.id}/[\w-]{{43}}\)"
    assert re.fullmatch(link, lines[1])
    assert lines[2] == "📎 a.exe（檔案類型不符，沒有傳送）"
    assert sent[0]["username"] == "客服小明"
    assert sent[0]["avatar_url"] == "https://cdn.discordapp.com/avatars/5/h.webp"
    assert sent[0]["allowed_mentions"].to_dict()["parse"] == []
    assert sent[0]["wait"] is True

    for message, discord_id in ((first, 1001), (second, 1002)):
        assert await env.status(message) is DeliveryStatus.SENT
        assert (message.discord_message_id, message.attempts) == (discord_id, 1)
    events = list(await env.db.scalars(select(TicketEvent).order_by(TicketEvent.id)))
    assert [(e.type, e.data) for e in events] == [
        ("message.updated", {"message_id": str(first.id), "delivery_status": "sent"}),
        ("message.updated", {"message_id": str(second.id), "delivery_status": "sent"}),
    ]
    # 已經送出的不會重送
    await env.deliver()
    assert len(env.webhook.sent) == 2


async def test_waits_for_attachment_check(env: Env) -> None:
    first = await env.message()
    attachment = await env.attach(first, "a.png", AttachmentStatus.PENDING)
    second = await env.message()
    await env.deliver()
    # 後面的訊息也不會插隊
    assert env.webhook.sent == []
    assert await env.status(second) is DeliveryStatus.PENDING

    attachment.status = AttachmentStatus.DONE
    await env.db.commit()
    await env.deliver()
    assert len(env.webhook.sent) == 2


async def test_retries_then_fails(env: Env) -> None:
    first = await env.message()
    second = await env.message()
    env.webhook.errors = [http_error(500)]
    with pytest.raises(discord.HTTPException):
        await env.deliver()
    assert await env.status(first) is DeliveryStatus.PENDING
    assert first.attempts == 1

    # 最後一次重試也失敗：標記為 failed，繼續送下一則
    env.webhook.errors = [http_error(500)]
    await env.deliver(last_attempt=True)
    assert await env.status(first) is DeliveryStatus.FAILED
    assert await env.status(second) is DeliveryStatus.SENT


async def test_bad_request_is_not_retried(env: Env) -> None:
    message = await env.message()
    env.webhook.errors = [http_error(400, "Invalid Form Body")]
    await env.deliver()
    assert await env.status(message) is DeliveryStatus.FAILED


async def test_rejected_username(env: Env) -> None:
    message = await env.message()
    error = {
        "code": 50035,
        "message": "Invalid Form Body",
        "errors": {"username": {"_errors": [{"code": "x", "message": "bad name"}]}},
    }
    env.webhook.errors = [http_error(400, error)]
    await env.deliver()
    assert await env.status(message) is DeliveryStatus.SENT
    assert env.webhook.sent[0]["username"] == delivery.FALLBACK_NAME


async def test_recreates_deleted_webhook(env: Env) -> None:
    message = await env.message()
    env.webhook.errors = [http_error(404, "Unknown Webhook")]
    await env.deliver()
    assert await env.status(message) is DeliveryStatus.SENT
    await env.db.refresh(env.ticket)
    assert (env.ticket.webhook_id, env.ticket.webhook_token) == (99, "new")


async def test_channel_deleted(env: Env) -> None:
    message = await env.message()
    env.webhook.errors = [http_error(404, "Unknown Webhook")]
    env.client.fetch_channel.side_effect = http_error(404, "Unknown Channel")
    await env.deliver()
    assert await env.status(message) is DeliveryStatus.FAILED


async def test_closed_ticket(env: Env) -> None:
    message = await env.message()
    env.ticket.status = TicketStatus.CLOSED
    await env.db.commit()
    await env.deliver()
    assert env.webhook.sent == []
    assert await env.status(message) is DeliveryStatus.FAILED


async def test_busy(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(delivery, "LOCK_WAIT", 0.1)
    await env.message()
    await env.redis.set(f"ticket-bot:lock:deliver:{env.ticket.id}", "other")
    # 取不到鎖要丟出例外讓 queue 稍後重試（持有鎖的 worker 可能已經當機）
    with pytest.raises(delivery.DeliveryBusy):
        await env.deliver()
    assert env.webhook.sent == []


def test_webhook_name() -> None:
    row = TicketUser(username="discordfan", global_name=None, nickname=None)
    assert delivery.webhook_name(row) == "di·scordfan"
    row.global_name = "Clyde 與 DISCORD"
    assert delivery.webhook_name(row) == "Cl·yde 與 DI·SCORD"
    row.global_name = "x" * 100
    assert delivery.webhook_name(row) == "x" * 80
    assert delivery.webhook_name(None) == delivery.FALLBACK_NAME
    # 沒有自訂頭像時用 Discord 的預設頭像
    assert delivery.avatar_url(None, 2**22 * 9) == "https://cdn.discordapp.com/embed/avatars/3.png"
