from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import aiohttp
import discord
import pytest
from fakeredis import FakeAsyncRedis
from multidict import CIMultiDict, CIMultiDictProxy
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yarl import URL

from bot.db.base import Base
from bot.db.engine import create_engine
from bot.db.models import (
    AttachmentStatus,
    Source,
    Ticket,
    TicketAttachment,
    TicketCategory,
    TicketEvent,
    TicketMessage,
    TicketUser,
)
from bot.storage import mirror, s3
from tests.fake_s3 import FakeS3
from tests.test_files import EXE, PNG


class FakeContent:
    def __init__(self, body: bytes) -> None:
        self.body = body

    async def iter_chunked(self, size: int) -> AsyncIterator[bytes]:
        # 跟真的連線一樣分成好幾塊送達
        for start in range(0, len(self.body), 100):
            yield self.body[start : start + 100]


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self.content = FakeContent(body)
        self.status = status

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, *_: object) -> None:
        pass

    def raise_for_status(self) -> None:
        if self.status >= 400:
            url = URL("https://cdn.example")
            info = aiohttp.RequestInfo(url, "GET", CIMultiDictProxy(CIMultiDict()), url)
            raise aiohttp.ClientResponseError(info, (), status=self.status)


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def fake_s3(monkeypatch: pytest.MonkeyPatch) -> FakeS3:
    client = FakeS3()

    @asynccontextmanager
    async def fake_client() -> AsyncIterator[FakeS3]:
        yield client

    monkeypatch.setattr(mirror, "s3_client", fake_client)
    monkeypatch.setattr(mirror, "get_redis", lambda: FakeAsyncRedis())
    return client


def serve(monkeypatch: pytest.MonkeyPatch, body: bytes | Exception) -> None:
    async def open_source(*_: Any) -> FakeResponse:
        if isinstance(body, Exception):
            raise body
        return FakeResponse(body)

    monkeypatch.setattr(mirror, "open_source", open_source)


async def add_attachment(db: AsyncSession, filename: str) -> TicketAttachment:
    category = TicketCategory(name="帳務")
    db.add(category)
    await db.flush()
    ticket = Ticket(
        category_id=category.id, source=Source.DISCORD, opener_id=5, subject="", content=""
    )
    db.add(ticket)
    await db.flush()
    message = TicketMessage(ticket_id=ticket.id, source=Source.DISCORD, author_id=5, content="")
    db.add(message)
    await db.flush()
    attachment = TicketAttachment(
        ticket_id=ticket.id,
        message_id=message.id,
        uploader_id=5,
        source=Source.DISCORD,
        filename=filename,
        content_type="image/png",
        size=0,
    )
    db.add(attachment)
    await db.commit()
    return attachment


async def mirror_once(db: AsyncSession, attachment: TicketAttachment) -> TicketAttachment:
    await mirror.mirror_attachment(db, cast(discord.Client, None), attachment.id)
    await db.refresh(attachment)
    return attachment


async def test_png_is_uploaded(
    db: AsyncSession, fake_s3: FakeS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve(monkeypatch, PNG)
    attachment = await mirror_once(db, await add_attachment(db, "../圖.png"))
    assert attachment.status is AttachmentStatus.DONE
    assert attachment.storage_key == f"tickets/1/{attachment.id}/_圖.png"
    assert fake_s3.objects[attachment.storage_key] == PNG
    assert attachment.size == len(PNG)
    assert attachment.attempts == 1
    event = await db.scalar(select(TicketEvent))
    assert event is not None and event.type == "attachment.updated"
    assert event.data == {"attachment_id": "1", "message_id": "1", "status": "done"}

    # 已經處理過的不會再處理
    serve(monkeypatch, RuntimeError("should not download"))
    assert (await mirror_once(db, attachment)).attempts == 1


async def test_disguised_executable_is_rejected(
    db: AsyncSession, fake_s3: FakeS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve(monkeypatch, EXE)
    attachment = await mirror_once(db, await add_attachment(db, "cat.png"))
    assert attachment.status is AttachmentStatus.REJECTED
    assert fake_s3.objects == {}


async def test_locked_attachment_is_retried(
    db: AsyncSession, fake_s3: FakeS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    redis = FakeAsyncRedis()
    monkeypatch.setattr(mirror, "get_redis", lambda: redis)
    serve(monkeypatch, PNG)
    attachment = await add_attachment(db, "a.png")
    # 另一個 worker 拿著鎖（或是它當機了，鎖還沒過期）：丟出例外讓 queue 稍後重試，
    # 不能直接結束，否則工作被 ack 之後附件會一直是 pending
    await redis.set(f"ticket-bot:lock:attachment:{attachment.id}", "other")
    with pytest.raises(mirror.AttachmentBusy):
        await mirror_once(db, attachment)
    await db.refresh(attachment)
    assert (attachment.status, attachment.attempts) == (AttachmentStatus.PENDING, 0)

    await redis.delete(f"ticket-bot:lock:attachment:{attachment.id}")
    assert (await mirror_once(db, attachment)).status is AttachmentStatus.DONE


async def test_deleted_source_fails(
    db: AsyncSession, fake_s3: FakeS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve(monkeypatch, mirror.SourceGone())
    attachment = await mirror_once(db, await add_attachment(db, "a.png"))
    assert attachment.status is AttachmentStatus.FAILED


async def test_multipart_upload(fake_s3: FakeS3, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(s3, "PART_SIZE", 250)
    body = bytes(range(256)) * 4

    sizes: list[int] = []
    upload_part = fake_s3.upload_part

    async def record(*, Body: bytes, **kwargs: Any) -> dict[str, str]:
        sizes.append(len(Body))
        return await upload_part(Body=Body, **kwargs)

    fake_s3.upload_part = record  # type: ignore[method-assign]
    size = await s3.upload_stream(
        cast(Any, fake_s3), "k", FakeContent(body).iter_chunked(0), content_type="x"
    )
    assert size == len(body)
    assert fake_s3.objects["k"] == body
    # 除了最後一塊，每一塊都剛好是 PART_SIZE（R2 要求大小相同）
    assert sizes == [250, 250, 250, 250, 24]

    async def broken() -> AsyncIterator[bytes]:
        yield b"x" * 300
        raise ConnectionError

    with pytest.raises(ConnectionError):
        await s3.upload_stream(cast(Any, fake_s3), "bad", broken(), content_type="x")
    assert fake_s3.aborted == ["bad"]


async def test_mirror_avatars(db: AsyncSession, fake_s3: FakeS3) -> None:
    attachment = await add_attachment(db, "a.png")
    ticket_id = attachment.ticket_id
    # 同一個人、同一個頭像出現在兩張單（只下載一次），另一個人的頭像已經不存在
    category = await db.scalar(select(TicketCategory))
    assert category is not None
    other = Ticket(category_id=category.id, source=Source.WEB, opener_id=5, subject="", content="")
    db.add(other)
    await db.flush()
    for tid in (ticket_id, other.id):
        db.add(
            TicketUser(
                ticket_id=tid,
                user_id=5,
                username="a",
                avatar_hash="h1",
                discord_avatar_url="https://ok",
            )
        )
    # 排在最前面、暫時失敗的頭像不會擋住後面的
    for user_id, avatar_hash, url in ((4, "h0", "https://flaky"), (6, "a_h2", "https://gone")):
        db.add(
            TicketUser(
                ticket_id=ticket_id,
                user_id=user_id,
                username="b",
                avatar_hash=avatar_hash,
                discord_avatar_url=url,
            )
        )
    await db.commit()

    # 比一塊（100 bytes）大，要讀到結束才完整
    avatar = bytes(range(256))
    responses = {
        "https://ok": FakeResponse(avatar),
        "https://flaky": FakeResponse(b"", 500),
        "https://gone": FakeResponse(b"", 404),
    }
    downloads: list[str] = []

    class FakeHTTP:
        def get(self, url: str) -> FakeResponse:
            downloads.append(url)
            return responses[url]

    assert await mirror.mirror_avatars(db, cast(Any, FakeHTTP()), cast(Any, fake_s3)) == 1
    assert downloads == ["https://flaky", "https://ok", "https://gone"]
    assert fake_s3.objects == {"avatars/5/h1.webp": avatar}
    rows = {(r.ticket_id, r.user_id): r for r in await db.scalars(select(TicketUser))}
    assert {r.avatar_key for r in rows.values() if r.user_id == 5} == {"avatars/5/h1.webp"}
    gone = rows[(ticket_id, 6)]
    assert (gone.avatar_key, gone.discord_avatar_url) == (None, None)
    flaky = rows[(ticket_id, 4)]
    assert (flaky.avatar_key, flaky.discord_avatar_url) == (None, "https://flaky")
    # 下次只重試暫時失敗的
    downloads.clear()
    assert await mirror.mirror_avatars(db, cast(Any, FakeHTTP()), cast(Any, fake_s3)) == 0
    assert downloads == ["https://flaky"]
