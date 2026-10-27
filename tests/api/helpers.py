import hashlib
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
from fakeredis import FakeAsyncRedis
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.fake_s3 import FakeS3

ORIGIN = "https://web.test"
USER_ID = 2**60
ADMIN_ROLE_ID = 10


def make_role(role_id: int, name: str = "身份組", *, assignable: bool = True) -> MagicMock:
    role = MagicMock(spec=discord.Role)
    role.id = role_id
    role.name = name
    role.color = discord.Colour(0xFF0000)
    role.position = 1
    role.hoist = False
    role.mentionable = False
    role.managed = False
    role.is_assignable.return_value = assignable
    role.is_default.return_value = False
    role.edit = AsyncMock(return_value=role)
    role.delete = AsyncMock()
    return role


def make_text_channel(channel_id: int, name: str = "general") -> MagicMock:
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = channel_id
    channel.name = name
    channel.category_id = None
    channel.position = 0
    channel.send = AsyncMock(return_value=SimpleNamespace(id=channel_id * 10))
    channel.create_webhook = AsyncMock(return_value=SimpleNamespace(id=7, token="webhook-token"))
    channel.set_permissions = AsyncMock()
    channel.delete = AsyncMock()
    # 開單訊息：fetch_message 取回後會被編輯
    opening = MagicMock()
    opening.embeds = [discord.Embed(title="開單").add_field(name="接手", value="尚未接手")]
    opening.edit = AsyncMock()
    channel.fetch_message = AsyncMock(return_value=opening)
    channel.opening = opening
    partial = MagicMock()
    partial.edit = AsyncMock(side_effect=lambda **_: SimpleNamespace(id=partial.message_id))
    partial.delete = AsyncMock()

    def get_partial_message(message_id: int) -> MagicMock:
        partial.message_id = message_id
        return partial

    channel.get_partial_message = MagicMock(side_effect=get_partial_message)
    channel.partial = partial
    return channel


def make_category_channel(channel_id: int, name: str = "客服單") -> MagicMock:
    channel = MagicMock(spec=discord.CategoryChannel)
    channel.id = channel_id
    channel.name = name
    channel.category_id = None
    channel.position = 0
    return channel


class FakeAsset:
    """discord.Asset：快照只會用到 key、url 和 replace()。"""

    def __init__(self, key: str, url: str) -> None:
        self.key = key
        self.url = url

    def replace(self, **_: Any) -> "FakeAsset":
        return self


def _fill_user(user: MagicMock, user_id: int) -> MagicMock:
    user.id = user_id
    user.name = "user"
    user.global_name = "User"
    user.mention = f"<@{user_id}>"
    user.display_name = "user"
    user.bot = False
    user.avatar = FakeAsset("avatarhash", "https://cdn.discordapp.com/avatars/a.webp")
    user.display_avatar = user.avatar
    return user


def make_user(user_id: int) -> MagicMock:
    """不在伺服器裡的使用者。"""
    return _fill_user(MagicMock(spec=discord.User), user_id)


def make_member(user_id: int, role_ids: list[int]) -> MagicMock:
    member = _fill_user(MagicMock(spec=discord.Member), user_id)
    member.nick = "暱稱"
    member.guild_avatar = None
    member.roles = [
        SimpleNamespace(id=role_id, hoist=True, position=1, name="管理員", color=discord.Colour(1))
        for role_id in role_ids
    ]
    member.guild_permissions = SimpleNamespace(administrator=False)
    return member


class FakeGuild:
    def __init__(self) -> None:
        self.members: dict[int, MagicMock] = {}
        self.me = discord.Object(1)
        self.default_role = discord.Object(0)
        # 開單時建立的頻道
        self.created_channels: list[MagicMock] = []
        self.create_text_channel = AsyncMock(side_effect=self._create_text_channel)
        self.fetch_ban = AsyncMock(
            side_effect=discord.NotFound(MagicMock(status=404), "not banned")
        )
        self.channels: list[MagicMock] = []
        self.roles: list[MagicMock] = [make_role(ADMIN_ROLE_ID, "管理員")]
        self.emojis: dict[int, object] = {}
        self.create_role = AsyncMock(side_effect=self._create_role)

    async def _create_role(self, *, name: str, **_: Any) -> MagicMock:
        role = make_role(len(self.roles) + 100, name)
        self.roles.append(role)
        return role

    async def _create_text_channel(self, name: str, **_: Any) -> MagicMock:
        channel = make_text_channel(9000 + len(self.created_channels), name)
        self.created_channels.append(channel)
        self.channels.append(channel)
        return channel

    def get_member(self, user_id: int) -> MagicMock | None:
        return self.members.get(user_id)

    def get_channel(self, channel_id: int) -> MagicMock | None:
        return next((c for c in self.channels if c.id == channel_id), None)

    async def fetch_channel(self, channel_id: int) -> MagicMock:
        # 快取裡沒有的頻道就是不存在
        raise discord.NotFound(MagicMock(status=404), "Unknown Channel")

    def get_role(self, role_id: int) -> MagicMock | None:
        return next((r for r in self.roles if r.id == role_id), None)

    def get_emoji(self, emoji_id: int) -> object | None:
        return self.emojis.get(emoji_id)


class FakeBot:
    def __init__(self) -> None:
        self.guild = FakeGuild()

    def get_guild(self, guild_id: int) -> FakeGuild:
        return self.guild

    async def fetch_user(self, user_id: int) -> MagicMock:
        return make_user(user_id)

    def is_ready(self) -> bool:
        return True


class Env:
    def __init__(
        self,
        client: AsyncClient,
        bot: FakeBot,
        sessions: FakeAsyncRedis,
        sessionmaker: async_sessionmaker[AsyncSession],
        app: FastAPI,
    ) -> None:
        self.client = client
        # 直接呼叫 ASGI app 用（例如只讀 SSE 的 header；httpx 會等回應全部讀完）
        self.app = app
        self.bot = bot
        self.guild = bot.guild
        self.sessions = sessions
        self.sessionmaker = sessionmaker
        # 放入 queue 的 mock（.kiq）
        self.sync_permissions = AsyncMock()
        self.verify = AsyncMock()
        self.deliver = AsyncMock()
        self.s3 = FakeS3()
        self.redis = FakeAsyncRedis(decode_responses=True)

    async def login_main_api(
        self, ttl: int = 100, user_id: int = USER_ID, status: str = "active"
    ) -> str:
        """模擬主要 API：建立 session 與授權碼（docs/auth.md 5.2、5.3）。"""
        session_hash = hashlib.sha256(f"main-session-{user_id}".encode()).hexdigest()
        session = {
            "internal_user_id": "user-id",
            "internal_account_status": status,
            "discord_id": str(user_id),
            "username": "user",
        }
        await self.sessions.set(f"auth:session:{session_hash}", json.dumps(session), ex=ttl)
        await self.sessions.set("auth:ticket_code:code", session_hash, ex=30)
        return session_hash

    async def exchange(self) -> Response:
        return await self.post("/auth/exchange", {"code": "code"})

    async def login(
        self, *, admin: bool = False, role_ids: list[int] | None = None, user_id: int = USER_ID
    ) -> None:
        """登入。admin、role_ids 都沒有指定時，是不在伺服器裡的使用者。"""
        self.guild.members.pop(user_id, None)
        if admin or role_ids is not None:
            self.add_member(user_id, (role_ids or []) + ([ADMIN_ROLE_ID] if admin else []))
        await self.login_main_api(user_id=user_id)
        assert (await self.exchange()).status_code == 200

    def add_member(self, user_id: int, role_ids: list[int]) -> MagicMock:
        """讓使用者在伺服器裡（不登入）。"""
        member = make_member(user_id, role_ids)
        self.guild.members[user_id] = member
        return member

    async def post(self, url: str, json: object) -> Response:
        return await self.client.post(url, json=json, headers={"Origin": ORIGIN})

    async def put(self, url: str, json: object) -> Response:
        return await self.client.put(url, json=json, headers={"Origin": ORIGIN})

    async def patch(self, url: str, json: object) -> Response:
        return await self.client.patch(url, json=json, headers={"Origin": ORIGIN})

    async def delete(self, url: str) -> Response:
        return await self.client.delete(
            url, headers={"Origin": ORIGIN, "Content-Type": "application/json"}
        )
