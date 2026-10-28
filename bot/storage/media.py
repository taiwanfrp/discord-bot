"""訊息裡的自訂表情與貼圖（docs/tickets.md 8.7）：記錄用到哪些，worker 再存到 S3。"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Sequence

import discord
import filetype
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.core.config import get_settings
from bot.db.models import DiscordMedia, MediaKind

# <:名稱:id>，動態的是 <a:名稱:id>。其他伺服器的表情也是同樣的格式，只看 id
EMOJI_PATTERN = re.compile(r"<(a?):\w{2,32}:(\d{15,21})>")
# 貼圖的格式 → 副檔名（APNG 也是 .png，瀏覽器都能直接播放）
STICKER_EXTENSIONS = {"png": "png", "apng": "png", "gif": "gif", "lottie": "json"}
EXTENSIONS = {
    MediaKind.EMOJI: frozenset({"gif", "webp"}),
    MediaKind.STICKER: frozenset(STICKER_EXTENSIONS.values()),
}
CONTENT_TYPES = {
    "gif": "image/gif",
    "webp": "image/webp",
    "png": "image/png",
    "json": "application/json",
}


def emojis_in(content: str) -> dict[int, str]:
    """內容裡的自訂表情：id → 副檔名（動態的存成 gif）。"""
    return {
        int(emoji_id): "gif" if animated else "webp"
        for animated, emoji_id in EMOJI_PATTERN.findall(content)
    }


def sticker_data(stickers: Sequence[discord.StickerItem]) -> list[dict[str, str]] | None:
    """存進 ticket_message.stickers 的資料。"""
    return [
        {"id": str(sticker.id), "name": sticker.name, "format": sticker.format.name}
        for sticker in stickers
    ] or None


def media_key(kind: MediaKind, media_id: int, extension: str) -> str:
    return f"{kind.value}s/{media_id}.{extension}"


def discord_url(kind: MediaKind, media_id: int, extension: str) -> str:
    if kind is MediaKind.EMOJI:
        return f"https://cdn.discordapp.com/emojis/{media_id}.{extension}"
    # GIF 的貼圖不在 cdn.discordapp.com（Discord 的 API 文件）
    host = "media.discordapp.net" if extension == "gif" else "cdn.discordapp.com"
    return f"https://{host}/stickers/{media_id}.{extension}"


def sticker_url(sticker_id: int, sticker_format: str) -> str | None:
    """網頁顯示貼圖用的網址（GET /stickers/{id}.{副檔名}）；不認得的格式是 None。"""
    extension = STICKER_EXTENSIONS.get(sticker_format)
    if extension is None:
        return None
    return f"{get_settings().public_base_url}/stickers/{sticker_id}.{extension}"


async def record_media(
    db: AsyncSession, content: str, stickers: list[dict[str, str]] | None = None
) -> None:
    """記錄還沒記錄過的表情、貼圖，由 mirror_media 存到 S3。

    在訊息 commit 之後呼叫，並且用另一個 session：同時有另一則訊息記錄同一個表情時，只有
    這裡失敗，不影響訊息，也不會讓呼叫的人手上的資料過期（rollback 會讓 session 裡的資料過期）。
    """
    wanted = {emoji_id: (MediaKind.EMOJI, ext) for emoji_id, ext in emojis_in(content).items()}
    for sticker in stickers or []:
        extension = STICKER_EXTENSIONS.get(sticker["format"])
        if extension is not None:
            wanted[int(sticker["id"])] = (MediaKind.STICKER, extension)
    if not wanted:
        return
    async with AsyncSession(db.bind) as media_db:
        existing = set(
            await media_db.scalars(select(DiscordMedia.id).where(DiscordMedia.id.in_(wanted)))
        )
        media_db.add_all(
            DiscordMedia(id=media_id, kind=kind, extension=extension)
            for media_id, (kind, extension) in wanted.items()
            if media_id not in existing
        )
        # 這次沒記錄到的，網頁先用 Discord 的網址，下次出現時再記錄
        with contextlib.suppress(IntegrityError):
            await media_db.commit()


def is_media(body: bytes, extension: str) -> bool:
    """下載到的內容跟副檔名相符，例如不是錯誤頁面。Lottie 是 JSON，沒有 magic bytes。"""
    if extension == "json":
        return body.lstrip().startswith(b"{")
    kind = filetype.guess(body)
    # 動態的 PNG 會被偵測成 apng
    return kind is not None and {"apng": "png"}.get(kind.extension, kind.extension) == extension
