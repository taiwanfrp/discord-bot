"""伺服器成員的名稱寫進 session store，給主要 API 顯示使用者名稱（docs/auth.md 5.6）。"""

import json
from collections.abc import AsyncIterator

import discord
from redis.asyncio import Redis

# 每 10 分鐘重新寫入；退出伺服器的人沒有再寫入，過了 TTL 就會消失
MEMBER_TTL = 60 * 60
BATCH = 1000


def member_key(member_id: int) -> str:
    return f"discord:member:{member_id}"


def member_fields(member: discord.Member) -> str:
    return json.dumps(
        {
            "username": member.name,
            "global_name": member.global_name,
            "nick": member.nick,
            "avatar": member.avatar.key if member.avatar else None,
            "guild_avatar": member.guild_avatar.key if member.guild_avatar else None,
        },
        ensure_ascii=False,
    )


async def sync_members(redis: Redis, members: AsyncIterator[discord.Member]) -> int:
    count = 0
    pipe = redis.pipeline(transaction=False)
    async for member in members:
        if member.bot:
            continue
        pipe.set(member_key(member.id), member_fields(member), ex=MEMBER_TTL)
        count += 1
        if count % BATCH == 0:
            await pipe.execute()
    await pipe.execute()
    return count
