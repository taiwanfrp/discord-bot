import io
import zipfile
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from fakeredis import FakeAsyncRedis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db.models import (
    AttachmentStatus,
    DiscordMedia,
    ExportStatus,
    MediaKind,
    Source,
    Ticket,
    TicketAttachment,
    TicketCategory,
    TicketExport,
    TicketMessage,
    TicketMessageRevision,
    TicketUser,
)
from bot.tickets import export
from tests.database import create_test_engine
from tests.fake_s3 import FakeS3
from tests.test_files import PNG
from tests.test_mirror import FakeResponse

OPENER = 2**60
STAFF = 2**60 + 1
EMOJI_ID = 983318658476552222
STICKER_ID = 1234567890123456789


class FakeHttp:
    """aiohttp.ClientSession：沒有設定的網址回傳 404。"""

    def __init__(self, bodies: dict[str, bytes] | None = None) -> None:
        self.bodies = bodies or {}
        self.requested: list[str] = []

    def get(self, url: str) -> FakeResponse:
        self.requested.append(url)
        body = self.bodies.get(url)
        return FakeResponse(body, 200) if body is not None else FakeResponse(b"", 404)


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = await create_test_engine(tmp_path)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


@pytest.fixture(autouse=True)
def redis(monkeypatch: pytest.MonkeyPatch) -> FakeAsyncRedis:
    client = FakeAsyncRedis()
    monkeypatch.setattr(export, "get_redis", lambda: client)
    return client


async def make_ticket(db: AsyncSession, s3: FakeS3, *, with_file: bool) -> Ticket:
    category = TicketCategory(name="帳務")
    db.add(category)
    await db.flush()
    ticket = Ticket(
        category_id=category.id,
        source=Source.WEB,
        opener_id=OPENER,
        subject="連不上 <隧道>",
        content="開單內容 <:_Gura:983318658476552222>",
        claimed_by_id=STAFF,
        created_at=datetime(2026, 10, 28, 6, 0, tzinfo=UTC),
    )
    db.add(ticket)
    await db.flush()
    db.add_all(
        [
            TicketUser(
                ticket_id=ticket.id,
                user_id=OPENER,
                username="opener",
                global_name="開單的人",
                avatar_key="avatars/opener.png",
            ),
            TicketUser(
                ticket_id=ticket.id,
                user_id=STAFF,
                username="staff",
                role_name="客服",
                role_color=0xFF0000,
                discord_avatar_url="https://cdn.discordapp.com/avatars/staff.png",
            ),
            DiscordMedia(
                id=EMOJI_ID,
                kind=MediaKind.EMOJI,
                extension="webp",
                storage_key=f"emojis/{EMOJI_ID}.webp",
                status=AttachmentStatus.DONE,
            ),
            # 還沒存到 S3：從 Discord 下載
            DiscordMedia(id=STICKER_ID, kind=MediaKind.STICKER, extension="png"),
        ]
    )
    s3.objects["avatars/opener.png"] = PNG
    s3.objects[f"emojis/{EMOJI_ID}.webp"] = PNG

    messages = [
        TicketMessage(
            ticket_id=ticket.id,
            source=Source.DISCORD,
            author_id=STAFF,
            content=f"請提供 **截圖** <@{OPENER}>",
            edited_at=datetime(2026, 10, 28, 6, 5, tzinfo=UTC),
            stickers=[{"id": str(STICKER_ID), "name": "Wumpus", "format": "png"}],
        ),
        TicketMessage(
            ticket_id=ticket.id,
            source=Source.DISCORD,
            author_id=OPENER,
            content="不小心傳的密碼",
            deleted_at=datetime(2026, 10, 28, 6, 6, tzinfo=UTC),
        ),
        # 轉發的訊息：附件顯示在被轉發的內容裡
        TicketMessage(
            ticket_id=ticket.id,
            source=Source.WEB,
            author_id=OPENER,
            content="附件",
            forwarded_content="被轉發的 **內容**",
            forwarded_from={
                "created_at": "2026-10-01T08:00:00+00:00",
                "guild_id": None,
                "channel_id": None,
                "message_id": None,
                "stickers": [{"id": str(STICKER_ID), "name": "Wumpus", "format": "png"}],
            },
        ),
    ]
    db.add_all(messages)
    await db.flush()
    db.add(
        TicketMessageRevision(
            message_id=messages[0].id,
            content="舊的內容",
            created_at=datetime(2026, 10, 28, 6, 1, tzinfo=UTC),
            replaced_at=datetime(2026, 10, 28, 6, 5, tzinfo=UTC),
        )
    )
    attachments = [
        TicketAttachment(
            ticket_id=ticket.id,
            message_id=messages[2].id,
            uploader_id=OPENER,
            source=Source.WEB,
            filename="病毒.exe",
            size=10,
            status=AttachmentStatus.REJECTED,
        ),
        # 被刪除的訊息裡的附件：只有客服匯出時才有
        TicketAttachment(
            ticket_id=ticket.id,
            message_id=messages[1].id,
            uploader_id=OPENER,
            source=Source.DISCORD,
            filename="secret.png",
            content_type="image/png",
            size=len(PNG),
            status=AttachmentStatus.DONE,
            storage_key="tickets/1/2/secret.png",
        ),
    ]
    s3.objects["tickets/1/2/secret.png"] = PNG
    if with_file:
        attachments.append(
            TicketAttachment(
                ticket_id=ticket.id,
                message_id=messages[2].id,
                uploader_id=OPENER,
                source=Source.WEB,
                filename='log:"1".txt',
                content_type="text/plain",
                size=3_000_000,
                status=AttachmentStatus.DONE,
                storage_key="tickets/1/3/log_1.txt",
            )
        )
        s3.objects["tickets/1/3/log_1.txt"] = b"x" * 3_000_000
    db.add_all(attachments)
    await db.commit()
    return ticket


async def run_export(
    db: AsyncSession,
    s3: FakeS3,
    ticket: Ticket,
    *,
    staff: bool,
    http: FakeHttp | None = None,
    last_attempt: bool = False,
) -> TicketExport:
    row = TicketExport(
        ticket_id=ticket.id,
        requested_by_id=STAFF if staff else OPENER,
        staff=staff,
        utc_offset=480,
    )
    db.add(row)
    await db.commit()
    http = http or FakeHttp(
        {
            "https://cdn.discordapp.com/avatars/staff.png": PNG,
            f"https://cdn.discordapp.com/stickers/{STICKER_ID}.png": PNG,
        }
    )
    await export.export_ticket(
        db, cast(Any, s3), cast(Any, http), row.id, last_attempt=last_attempt
    )
    await db.refresh(row)
    return row


async def test_html_export_for_opener(db: AsyncSession) -> None:
    s3 = FakeS3()
    ticket = await make_ticket(db, s3, with_file=False)
    row = await run_export(db, s3, ticket, staff=False)
    assert row.status is ExportStatus.DONE
    assert row.format == "html"
    assert row.storage_key == f"exports/{row.id}/ticket-{ticket.id}.html"
    page = s3.objects[row.storage_key].decode()
    assert row.size == len(s3.objects[row.storage_key])

    assert "連不上 &lt;隧道&gt;" in page
    # 時間依照網頁送來的時差（+8）
    assert ">2026/10/28 14:00</time>" in page
    assert "<strong>截圖</strong>" in page
    assert '<span class="md-mention">@開單的人</span>' in page
    assert 'style="color: #ff0000"' in page
    # 每張圖只寫一次 base64，用 CSS class 引用
    assert page.count("data:image/png;base64,") == 4
    # 網頁的背景圖片
    assert page.count("data:image/webp;base64,") == 1
    assert f'class="md-emoji emoji-{EMOJI_ID}"' in page
    assert f'class="sticker sticker-{STICKER_ID}"' in page
    assert "已轉發" in page and "被轉發的 <strong>內容</strong>" in page
    assert ">2026/10/01 16:00</time>" in page
    # 沒有通過檢查的附件只顯示檔名與原因
    assert "病毒.exe" in page and "不符合附件規則" in page
    # 開單者看不到被刪除的訊息、它的附件與編輯紀錄
    assert "此訊息已被刪除</p>" in page
    assert "不小心傳的密碼" not in page and "secret.png" not in page
    assert "舊的內容" not in page


async def test_zip_export_for_staff(db: AsyncSession) -> None:
    s3 = FakeS3()
    ticket = await make_ticket(db, s3, with_file=True)
    row = await run_export(db, s3, ticket, staff=True)
    assert row.format == "zip"
    assert row.storage_key is not None
    archive = zipfile.ZipFile(io.BytesIO(s3.objects[row.storage_key]))
    assert archive.testzip() is None
    names = set(archive.namelist())
    assert names == {
        "transcript.html",
        "assets/background.webp",
        f"assets/avatars/{OPENER}.png",
        f"assets/avatars/{STAFF}.png",
        f"assets/emojis/{EMOJI_ID}.png",
        f"assets/stickers/{STICKER_ID}.png",
        "assets/attachments/2/secret.png",
        "assets/attachments/3/log_1.txt",
    }
    assert archive.read("assets/attachments/3/log_1.txt") == b"x" * 3_000_000
    page = archive.read("transcript.html").decode()
    assert "data:image" not in page
    assert f'url("assets/avatars/{OPENER}.png")' in page
    assert '<img src="assets/attachments/2/secret.png"' in page
    assert 'href="assets/attachments/3/log_1.txt"' in page
    assert "log:&#34;1&#34;.txt" in page
    # 客服看得到被刪除的訊息與編輯紀錄
    assert "不小心傳的密碼" in page and "只有客服看得到原本的內容" in page
    assert "編輯紀錄（1）" in page and "舊的內容" in page


async def test_only_staff_files_make_a_zip(db: AsyncSession) -> None:
    """被刪除訊息裡的附件只有客服看得到，所以客服是 ZIP、開單者是 HTML。"""
    s3 = FakeS3()
    ticket = await make_ticket(db, s3, with_file=False)
    assert (await run_export(db, s3, ticket, staff=True)).format == "zip"
    assert (await run_export(db, s3, ticket, staff=False)).format == "html"


async def test_missing_images_fall_back(db: AsyncSession) -> None:
    """S3 與 Discord 都拿不到的頭像、表情：預設頭像、表情顯示名稱。"""
    s3 = FakeS3()
    ticket = await make_ticket(db, s3, with_file=False)
    s3.objects.clear()
    http = FakeHttp({f"https://cdn.discordapp.com/embed/avatars/{(OPENER >> 22) % 6}.png": PNG})
    row = await run_export(db, s3, ticket, staff=False, http=http)
    assert row.storage_key is not None
    page = s3.objects[row.storage_key].decode()
    # 兩個人都用預設頭像（剛好是同一張）
    assert (OPENER >> 22) % 6 == (STAFF >> 22) % 6
    assert page.count("data:image/png;base64,") == 2
    assert "開單內容 :_Gura:" in page
    assert "貼圖：Wumpus" in page


async def test_waits_for_pending_attachments(db: AsyncSession) -> None:
    s3 = FakeS3()
    ticket = await make_ticket(db, s3, with_file=False)
    message = await db.scalar(select(TicketMessage).where(TicketMessage.source == Source.WEB))
    assert message is not None
    db.add(
        TicketAttachment(
            ticket_id=ticket.id,
            message_id=message.id,
            uploader_id=OPENER,
            source=Source.DISCORD,
            filename="還在下載.png",
            size=1,
        )
    )
    await db.commit()
    with pytest.raises(export.ExportNotReady):
        await run_export(db, s3, ticket, staff=False)
    row = await db.scalar(select(TicketExport))
    assert row is not None and row.status is ExportStatus.PENDING

    # 最後一次就不等了
    row = await run_export(db, s3, ticket, staff=False, last_attempt=True)
    assert row.status is ExportStatus.DONE
    assert row.storage_key is not None
    assert "尚未處理完成" in s3.objects[row.storage_key].decode()


async def test_busy_and_finished_exports(db: AsyncSession, redis: FakeAsyncRedis) -> None:
    s3 = FakeS3()
    ticket = await make_ticket(db, s3, with_file=False)
    row = TicketExport(ticket_id=ticket.id, requested_by_id=OPENER, staff=False, utc_offset=0)
    db.add(row)
    await db.commit()
    await redis.set(f"ticket-bot:lock:export:{row.id}", "other")
    with pytest.raises(export.ExportBusy):
        await export.export_ticket(
            db, cast(Any, s3), cast(Any, FakeHttp()), row.id, last_attempt=False
        )
    await redis.delete(f"ticket-bot:lock:export:{row.id}")

    await export.give_up(db, row.id)
    assert row.status is ExportStatus.FAILED
    # 已經不是 pending：不會再產生
    await export.export_ticket(db, cast(Any, s3), cast(Any, FakeHttp()), row.id, last_attempt=False)
    assert not any(key.startswith("exports/") for key in s3.objects)


async def test_delete_expired(db: AsyncSession) -> None:
    s3 = FakeS3()
    ticket = await make_ticket(db, s3, with_file=False)
    old = await run_export(db, s3, ticket, staff=False)
    old.created_at = datetime.now(UTC) - timedelta(hours=25)
    await db.commit()
    new = await run_export(db, s3, ticket, staff=False)

    assert await export.delete_expired(db, cast(Any, s3)) == 1
    assert old.storage_key not in s3.objects and new.storage_key in s3.objects
    assert [row.id for row in await db.scalars(select(TicketExport))] == [new.id]
