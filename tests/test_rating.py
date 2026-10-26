import re
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db.base import Base
from bot.db.engine import create_engine
from bot.db.models import Source, Ticket, TicketCategory, TicketStatus
from bot.db.types import utcnow
from bot.tickets import rating
from bot.tickets.errors import TicketError

OPENER = 5


@pytest.fixture
async def sessionmaker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(rating, "get_sessionmaker", lambda: maker)
    async with maker() as db:
        category = TicketCategory(name="帳務")
        db.add(category)
        await db.flush()
        db.add(
            Ticket(
                category_id=category.id,
                source=Source.DISCORD,
                status=TicketStatus.CLOSED,
                opener_id=OPENER,
                subject="",
                content="",
                closed_at=utcnow(),
            )
        )
        await db.commit()
    yield maker
    await engine.dispose()


def interaction(user_id: int, *, message: object | None = None) -> Any:
    response = MagicMock(
        send_modal=AsyncMock(),
        send_message=AsyncMock(),
        edit_message=AsyncMock(),
        is_done=MagicMock(return_value=False),
    )
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        response=response,
        followup=MagicMock(send=AsyncMock()),
        message=message,
    )


def test_parse_rating() -> None:
    assert rating.parse_rating(" 5 ") == 5
    # 中文輸入法常打出全形數字
    assert rating.parse_rating("３") == 3
    for value in ("0", "6", "a", ""):
        with pytest.raises(TicketError) as error:
            rating.parse_rating(value)
        assert error.value.status_code == 422


async def test_button_custom_id() -> None:
    button = rating.RateButton(12)
    match = button.template.fullmatch(button.custom_id)
    assert match is not None
    item = cast(discord.ui.Item[Any], None)
    restored = await rating.RateButton.from_custom_id(cast(Any, None), item, match)
    assert restored.ticket_id == 12
    assert not re.fullmatch(button.template, "ticket:close")


async def test_button_opens_modal(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    clicked = interaction(OPENER)
    await rating.RateButton(1).callback(clicked)
    modal = clicked.response.send_modal.call_args.args[0]
    assert isinstance(modal, rating.RatingModal) and modal.ticket_id == 1

    # 不是開單者：回覆錯誤，不會丟出例外
    other = interaction(OPENER + 1)
    await rating.RateButton(1).callback(other)
    other.response.send_modal.assert_not_awaited()
    assert other.response.send_message.call_args.args[0] == "只有開單者可以評分"


def dm() -> Any:
    """關單私訊（只有 embed 會被用到）。"""
    embed = discord.Embed(title="客服單 #1 已關閉", description="歡迎為這次的客服評分。")
    return SimpleNamespace(embeds=[embed.add_field(name="分類", value="帳務")])


def edited(clicked: Any) -> tuple[list[tuple[str, str]], list[str | None]]:
    """私訊被改成什麼：embed 的欄位與按鈕的連結。"""
    kwargs = clicked.response.edit_message.call_args.kwargs
    [embed] = kwargs["embeds"]
    assert embed.description is None
    fields = [(field.name, field.value) for field in embed.fields]
    return fields, [item.url for item in kwargs["view"].children]


async def test_submit(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    modal = rating.RatingModal(1)
    modal.score._value = "4"
    modal.feedback._value = " 很快就解決了 "
    message = dm()
    submitted = interaction(OPENER, message=message)
    await modal.on_submit(submitted)

    # 私訊加上評分與評語，評分按鈕拿掉，只留下紀錄的連結
    assert edited(submitted) == (
        [("分類", "帳務"), ("評分", "★★★★☆（4 分）"), ("評語", "很快就解決了")],
        ["https://web.test/tickets/1"],
    )
    # 原本的訊息物件不會被改到
    assert message.embeds[0].description is not None
    submitted.followup.send.assert_awaited_once()
    async with sessionmaker() as db:
        ticket = await db.get_one(Ticket, 1)
        assert (ticket.rating, ticket.feedback) == (4, "很快就解決了")
        assert ticket.rated_at is not None

    # 已經評分過了（例如先在網頁評分）：按下按鈕時把私訊改成顯示評分
    clicked = interaction(OPENER, message=dm())
    await rating.RateButton(1).callback(clicked)
    clicked.response.send_modal.assert_not_awaited()
    assert edited(clicked)[0][1:] == [("評分", "★★★★☆（4 分）"), ("評語", "很快就解決了")]
    assert clicked.followup.send.call_args.args[0] == "已經評分過了"


async def test_submit_invalid_score(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    modal = rating.RatingModal(1)
    modal.score._value = "9"
    with pytest.raises(TicketError):
        await modal.on_submit(interaction(OPENER))
    async with sessionmaker() as db:
        assert (await db.get_one(Ticket, 1)).rating is None
