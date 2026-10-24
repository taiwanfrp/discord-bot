import hashlib
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

import discord
from discord.ext import commands
from fastapi import Depends, HTTPException, Request
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.core.config import get_settings
from bot.core.permissions import Access, get_access
from bot.core.redis import get_redis, get_session_redis
from bot.db.engine import get_sessionmaker


def get_bot(request: Request) -> commands.Bot:
    bot: commands.Bot = request.app.state.bot
    return bot


def get_guild(bot: Annotated[commands.Bot, Depends(get_bot)]) -> discord.Guild:
    guild = bot.get_guild(get_settings().guild_id)
    if guild is None:
        # bot 還沒連上 Discord
        raise HTTPException(503, "Discord is not ready")
    return guild


async def get_db() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        yield session


BotDep = Annotated[commands.Bot, Depends(get_bot)]
GuildDep = Annotated[discord.Guild, Depends(get_guild)]
DBDep = Annotated[AsyncSession, Depends(get_db)]
RedisDep = Annotated[Redis, Depends(get_redis)]
SessionRedisDep = Annotated[Redis, Depends(get_session_redis)]


def ticket_session_key(token: str) -> str:
    # 只存 ticket session 的雜湊，bot 的 Redis 外洩也拿不到能用的 cookie
    return f"ticket-bot:ticket_session:{hashlib.sha256(token.encode()).hexdigest()}"


async def authenticate(request: Request, redis: RedisDep, session_redis: SessionRedisDep) -> int:
    """ticket session → 主要 API 的 session → Discord ID（docs/auth.md 3.2）。"""
    token = request.cookies.get(get_settings().auth_cookie_name)
    if not token:
        raise HTTPException(401, "Not logged in")
    try:
        api_session_hash = await redis.hget(ticket_session_key(token), "api_session_hash")
        # 用了 decode_responses，查得到時一定是 str
        if not isinstance(api_session_hash, str):
            raise HTTPException(401, "Session expired")
        discord_id = await session_redis.hget(f"session:{api_session_hash}", "discord_id")
    except RedisError as error:
        # 無法確認 session 時不可以放行
        raise HTTPException(503, "Session store unavailable") from error
    if not isinstance(discord_id, str):
        raise HTTPException(401, "Session expired")
    return int(discord_id)


class UserCache:
    """不在伺服器裡的使用者，用 REST API 取得資料，快取幾分鐘。"""

    def __init__(self, ttl: float = 300) -> None:
        self.ttl = ttl
        self._items: dict[int, tuple[float, discord.User]] = {}

    async def get(self, bot: commands.Bot, user_id: int) -> discord.User:
        now = time.monotonic()
        cached = self._items.get(user_id)
        if cached is not None and cached[0] > now:
            return cached[1]
        user = await bot.fetch_user(user_id)
        self._items = {k: v for k, v in self._items.items() if v[0] > now}
        self._items[user_id] = (now + self.ttl, user)
        return user


@dataclass(frozen=True)
class Viewer:
    """目前登入的使用者。"""

    user: discord.User | discord.Member
    # 不在伺服器裡時是 None
    member: discord.Member | None
    access: Access

    @property
    def id(self) -> int:
        return self.user.id


async def resolve_viewer(
    request: Request, discord_id: int, bot: commands.Bot, guild: discord.Guild, db: AsyncSession
) -> Viewer:
    member = guild.get_member(discord_id)
    user: discord.User | discord.Member
    if member is not None:
        user = member
    else:
        cache: UserCache = request.app.state.user_cache
        try:
            user = await cache.get(bot, discord_id)
        except discord.NotFound as error:
            raise HTTPException(401, "Discord account not found") from error
        except discord.HTTPException as error:
            raise HTTPException(503, "Discord is unavailable") from error
    return Viewer(user=user, member=member, access=await get_access(db, member))


async def get_viewer(
    request: Request,
    discord_id: Annotated[int, Depends(authenticate)],
    bot: BotDep,
    guild: GuildDep,
    db: DBDep,
) -> Viewer:
    return await resolve_viewer(request, discord_id, bot, guild, db)


def get_admin(viewer: Annotated[Viewer, Depends(get_viewer)]) -> Viewer:
    if not viewer.access.is_admin:
        raise HTTPException(403, "Admin only")
    return viewer


ViewerDep = Annotated[Viewer, Depends(get_viewer)]
AdminDep = Annotated[Viewer, Depends(get_admin)]
