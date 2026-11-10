import json
from collections.abc import AsyncIterator
from unittest.mock import MagicMock

import discord
import pytest
from fakeredis import FakeAsyncRedis

from bot.core import members
from bot.core.members import MEMBER_TTL, member_key


def member(member_id: int, *, bot: bool = False, nick: str | None = None) -> discord.Member:
    fake = MagicMock(spec=discord.Member)
    fake.id = member_id
    fake.bot = bot
    fake.name = f"user{member_id}"
    fake.global_name = f"User {member_id}"
    fake.nick = nick
    fake.avatar = MagicMock(key="abc")
    fake.guild_avatar = None
    return fake


async def iterate(*items: discord.Member) -> AsyncIterator[discord.Member]:
    for item in items:
        yield item


async def test_sync_members_writes_names_and_skips_bots() -> None:
    redis = FakeAsyncRedis(decode_responses=True)
    count = await members.sync_members(redis, iterate(member(1, nick="暱稱"), member(2, bot=True)))

    assert count == 1
    assert await redis.keys() == [member_key(1)]
    stored = await redis.get(member_key(1))
    assert stored is not None
    assert json.loads(stored) == {
        "username": "user1",
        "global_name": "User 1",
        "nick": "暱稱",
        "avatar": "abc",
        "guild_avatar": None,
    }
    assert 0 < await redis.ttl(member_key(1)) <= MEMBER_TTL


async def test_sync_members_in_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(members, "BATCH", 2)
    redis = FakeAsyncRedis(decode_responses=True)

    assert await members.sync_members(redis, iterate(member(1), member(2), member(3))) == 3
    assert sorted(await redis.keys()) == sorted(member_key(i) for i in (1, 2, 3))
