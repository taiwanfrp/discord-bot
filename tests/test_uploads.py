from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
)
from bot.db.types import utcnow
from bot.storage import uploads
from tests.fake_s3 import FakeS3
from tests.test_files import EXE, PNG


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


async def add_upload(
    db: AsyncSession, s3: FakeS3, body: bytes | None, *, filename: str = "a.png", size: int = 0
) -> TicketAttachment:
    """網頁上傳完成的附件；body 是 None 表示 S3 上沒有檔案。"""
    ticket = await db.scalar(select(Ticket))
    if ticket is None:
        category = TicketCategory(name="帳務")
        db.add(category)
        await db.flush()
        ticket = Ticket(
            category_id=category.id, source=Source.WEB, opener_id=5, subject="", content=""
        )
        db.add(ticket)
        await db.flush()
    attachment = TicketAttachment(
        ticket_id=ticket.id,
        uploader_id=5,
        source=Source.WEB,
        filename=filename,
        size=size or len(body or b""),
        uploaded_at=utcnow(),
    )
    db.add(attachment)
    await db.flush()
    attachment.storage_key = f"tickets/{ticket.id}/{attachment.id}/{filename}"
    if body is not None:
        s3.objects[attachment.storage_key] = body
    await db.commit()
    return attachment


async def verify(db: AsyncSession, s3: FakeS3, attachment: TicketAttachment) -> int | None:
    result = await uploads.verify_attachment(db, cast(Any, s3), attachment.id)
    await db.refresh(attachment)
    return result


async def test_valid_file(db: AsyncSession) -> None:
    s3 = FakeS3()
    attachment = await add_upload(db, s3, PNG)
    assert await verify(db, s3, attachment) is None
    assert attachment.status is AttachmentStatus.DONE
    assert attachment.attempts == 1
    # 依照檔案內容判斷，不相信瀏覽器
    assert attachment.content_type == "image/png"
    event = await db.scalar(select(TicketEvent))
    assert event is not None and event.type == "attachment.updated"
    # 處理過的不會再處理
    assert await verify(db, s3, attachment) is None
    assert attachment.attempts == 1


async def test_returns_waiting_ticket(db: AsyncSession) -> None:
    s3 = FakeS3()
    attachment = await add_upload(db, s3, PNG)
    message = TicketMessage(
        ticket_id=attachment.ticket_id, source=Source.WEB, author_id=5, content=""
    )
    db.add(message)
    await db.flush()
    attachment.message_id = message.id
    await db.commit()
    # 訊息在等這個附件，要接著送出
    assert await verify(db, s3, attachment) == attachment.ticket_id


@pytest.mark.parametrize(
    ("body", "size"),
    [
        # 偽裝成圖片的執行檔
        (EXE, 0),
        # 上傳的大小跟申請時不同
        (PNG, len(PNG) + 1),
    ],
)
async def test_rejected(db: AsyncSession, body: bytes, size: int) -> None:
    s3 = FakeS3()
    attachment = await add_upload(db, s3, body, size=size)
    key = attachment.storage_key
    await verify(db, s3, attachment)
    assert attachment.status is AttachmentStatus.REJECTED
    assert attachment.storage_key is None
    assert key not in s3.objects


async def test_missing_file(db: AsyncSession) -> None:
    s3 = FakeS3()
    attachment = await add_upload(db, s3, None, size=10)
    await verify(db, s3, attachment)
    assert attachment.status is AttachmentStatus.FAILED


async def test_not_uploaded_yet(db: AsyncSession) -> None:
    s3 = FakeS3()
    attachment = await add_upload(db, s3, PNG)
    attachment.uploaded_at = None
    await db.commit()
    assert await verify(db, s3, attachment) is None
    assert (attachment.status, attachment.attempts) == (AttachmentStatus.PENDING, 0)


async def test_delete_unsent(db: AsyncSession) -> None:
    s3 = FakeS3()
    old = await add_upload(db, s3, PNG)
    # 分塊上傳到一半
    stalled = await add_upload(db, s3, None, size=10)
    stalled.upload_id = (await s3.create_multipart_upload(Key=stalled.storage_key or ""))[
        "UploadId"
    ]
    stalled.uploaded_at = None
    recent = await add_upload(db, s3, PNG)
    sent = await add_upload(db, s3, PNG)
    message = TicketMessage(ticket_id=sent.ticket_id, source=Source.WEB, author_id=5, content="")
    db.add(message)
    await db.flush()
    sent.message_id = message.id
    for attachment in (old, stalled, sent):
        attachment.created_at = utcnow() - timedelta(days=2)
    await db.commit()

    assert await uploads.delete_unsent(db, cast(Any, s3)) == 2
    ids = set(await db.scalars(select(TicketAttachment.id)))
    assert ids == {recent.id, sent.id}
    assert old.storage_key not in s3.objects
    assert stalled.upload_id not in s3.uploads
    assert sent.storage_key in s3.objects
