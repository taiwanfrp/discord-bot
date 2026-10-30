"""匯出客服單（docs/tickets.md 8.8）。

沒有附件（有檔案的）時是一個 HTML 檔，頭像、表情、貼圖用 base64 寫在裡面；有附件時是 ZIP：
transcript.html 加上 assets 資料夾。由 worker 產生後存到 S3，網頁再用 presigned URL 下載。
HTML 用 bot/templates/export.html.j2 產生，這裡只準備資料。
"""

from __future__ import annotations

import base64
import contextlib
import logging
import re
import zipfile
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import aiohttp
import filetype
from botocore.exceptions import ClientError
from jinja2 import Environment, PackageLoader, StrictUndefined
from markupsafe import Markup, escape
from redis.exceptions import LockError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.core.config import get_settings
from bot.core.redis import get_redis
from bot.db.models import (
    AttachmentStatus,
    DeliveryStatus,
    DiscordMedia,
    ExportStatus,
    MediaKind,
    Ticket,
    TicketAttachment,
    TicketCategory,
    TicketExport,
    TicketMember,
    TicketMessage,
    TicketMessageRevision,
    TicketStatus,
    TicketUser,
)
from bot.db.types import utcnow
from bot.storage.files import content_disposition
from bot.storage.media import STICKER_EXTENSIONS, discord_url, emojis_in
from bot.storage.mirror import download_small_file
from bot.storage.s3 import upload_stream
from bot.tickets import markdown
from bot.tickets.history import visible_attachments, visible_revisions

if TYPE_CHECKING:
    from types_aiobotocore_s3 import S3Client

log = logging.getLogger(__name__)

templates = Environment(
    loader=PackageLoader("bot", "templates"),
    autoescape=True,
    undefined=StrictUndefined,
    keep_trailing_newline=True,
)
CSS = templates.loader.get_source(templates, "export.css")[0]  # type: ignore[union-attr]
# 匯出的檔案保留多久
EXPORT_TTL = timedelta(hours=24)
# 要比 worker 的 idle_timeout（30 分鐘）長
LOCK_TIMEOUT = 35 * 60
CHUNK_SIZE = 1024 * 1024
# ZIP 裡 HTML 的檔名
TRANSCRIPT = "transcript.html"

# 狀態徽章：跟網頁的 TICKET_STATUS 一樣
STATUS = {
    "open": {"label": "待處理", "color": "warning", "icon": "clock"},
    "claimed": {"label": "處理中", "color": "info", "icon": "user"},
    "closed": {"label": "已關閉", "color": "neutral", "icon": "lock"},
    "failed": {"label": "開單失敗", "color": "error", "icon": "warning"},
}
ATTACHMENT_TEXT = {
    AttachmentStatus.PENDING: "尚未處理完成，沒有包含在匯出的檔案裡",
    AttachmentStatus.REJECTED: "不符合附件規則",
    AttachmentStatus.FAILED: "無法保存",
}


class ExportNotReady(Exception):
    """還有附件沒存到 S3，稍後重試。"""


class ExportBusy(Exception):
    """另一個 worker 正在產生這個匯出。"""


@dataclass
class Image:
    """頭像、表情、貼圖：HTML 用 CSS class 引用，同一張圖只寫一次。"""

    css_class: str
    # ZIP 裡的路徑
    path: str
    content_type: str
    data: bytes


@dataclass
class Transcript:
    ticket: Ticket
    category: TicketCategory | None
    users: dict[int, TicketUser]
    members: list[TicketMember]
    messages: list[TicketMessage]
    attachments: dict[int, list[TicketAttachment]]
    revisions: dict[int, list[TicketMessageRevision]]
    # 用到的自訂表情：id → 副檔名；貼圖：id → 副檔名（Lottie 不顯示，不在這裡）
    emojis: dict[int, str]
    stickers: dict[int, str]
    # S3 上的備份
    media: dict[int, DiscordMedia]

    def all_attachments(self) -> list[TicketAttachment]:
        return [attachment for rows in self.attachments.values() for attachment in rows]

    def files(self) -> list[TicketAttachment]:
        """有檔案、要放進 ZIP 的附件。"""
        return [
            attachment
            for attachment in self.all_attachments()
            if attachment.status is AttachmentStatus.DONE and attachment.storage_key is not None
        ]


async def load(db: AsyncSession, ticket: Ticket, *, staff: bool) -> Transcript:
    messages = list(
        await db.scalars(
            select(TicketMessage)
            .where(TicketMessage.ticket_id == ticket.id)
            .order_by(TicketMessage.id)
        )
    )
    revisions = await visible_revisions(db, messages, staff=staff)
    category = await db.get(TicketCategory, ticket.category_id)

    texts = [ticket.content, category.emoji if category and category.emoji else ""]
    stickers: dict[int, str] = {}
    for message in messages:
        if staff or message.deleted_at is None:
            texts.append(message.content)
            for sticker in message.stickers or []:
                extension = STICKER_EXTENSIONS.get(sticker["format"])
                if extension is not None and extension != "json":
                    stickers[int(sticker["id"])] = extension
    texts += [revision.content for rows in revisions.values() for revision in rows]
    emojis = {key: value for text in texts for key, value in emojis_in(text).items()}

    media_ids = set(emojis) | set(stickers)
    return Transcript(
        ticket=ticket,
        category=category,
        users={
            row.user_id: row
            for row in await db.scalars(select(TicketUser).where(TicketUser.ticket_id == ticket.id))
        },
        members=list(
            await db.scalars(
                select(TicketMember)
                .where(TicketMember.ticket_id == ticket.id)
                .order_by(TicketMember.added_at)
            )
        ),
        messages=messages,
        attachments=await visible_attachments(db, messages, staff=staff),
        revisions=revisions,
        emojis=emojis,
        stickers=stickers,
        media={
            row.id: row
            for row in await db.scalars(select(DiscordMedia).where(DiscordMedia.id.in_(media_ids)))
        },
    )


async def read_object(s3: S3Client, key: str) -> bytes:
    response = await s3.get_object(Bucket=get_settings().s3_bucket, Key=key)
    async with response["Body"] as body:
        return await body.read()


async def fetch_image(
    s3: S3Client, http: aiohttp.ClientSession, key: str | None, urls: Sequence[str]
) -> tuple[bytes, filetype.Type] | None:
    """依序試 S3 上的備份與 Discord 的網址，都拿不到（或不是圖片）時回傳 None。"""
    if key is not None:
        try:
            data = await read_object(s3, key)
        except ClientError:
            log.warning("Failed to read %s from S3", key, exc_info=True)
        else:
            if (kind := filetype.guess(data)) is not None:
                return data, kind
    for url in urls:
        try:
            downloaded = await download_small_file(http, url)
        except (aiohttp.ClientError, TimeoutError):
            log.warning("Failed to download %s", url, exc_info=True)
            continue
        if downloaded is not None and (kind := filetype.guess(downloaded)) is not None:
            return downloaded, kind
    return None


def default_avatar_url(user_id: int) -> str:
    return f"https://cdn.discordapp.com/embed/avatars/{(user_id >> 22) % 6}.png"


def display_name(user: TicketUser | None) -> str:
    if user is None:
        return "未知使用者"
    return user.nickname or user.global_name or user.username


def format_size(size: int) -> str:
    """跟網頁的 formatFileSize 一樣。"""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError


# Windows 的檔名不能有這些字元，解壓縮時會失敗
_WINDOWS_RESERVED = re.compile(r'[<>:"|?*]')


def attachment_path(attachment: TicketAttachment) -> str:
    """ZIP 裡的路徑：附件 id 當作資料夾，同名的檔案不會互相覆蓋。"""
    assert attachment.storage_key is not None
    # storage_key 的最後一段是清理過的檔名（bot/storage/files.py 的 safe_filename）
    name = _WINDOWS_RESERVED.sub("_", attachment.storage_key.rsplit("/", 1)[-1])
    return f"assets/attachments/{attachment.id}/{name}"


class Renderer:
    """把資料整理成範本用的格式，再產生 HTML。"""

    def __init__(
        self,
        transcript: Transcript,
        *,
        staff: bool,
        tz: timezone,
        now: datetime,
        as_zip: bool,
    ) -> None:
        self.transcript = transcript
        self.staff = staff
        self.tz = tz
        self.now = now
        self.as_zip = as_zip
        self.images: dict[str, Image] = {}

    async def load_images(self, s3: S3Client, http: aiohttp.ClientSession) -> None:
        for user in self.transcript.users.values():
            urls = [user.discord_avatar_url] if user.discord_avatar_url else []
            urls.append(default_avatar_url(user.user_id))
            path = f"assets/avatars/{user.user_id}"
            await self._add_image(s3, http, f"av-{user.user_id}", path, user.avatar_key, urls)
        for kind, items in (
            (MediaKind.EMOJI, self.transcript.emojis),
            (MediaKind.STICKER, self.transcript.stickers),
        ):
            for media_id, extension in items.items():
                media = self.transcript.media.get(media_id)
                key = media.storage_key if media and media.status is AttachmentStatus.DONE else None
                await self._add_image(
                    s3,
                    http,
                    f"{kind.value}-{media_id}",
                    f"assets/{kind.value}s/{media_id}",
                    key,
                    [discord_url(kind, media_id, extension)],
                )

    async def _add_image(
        self,
        s3: S3Client,
        http: aiohttp.ClientSession,
        css_class: str,
        path: str,
        key: str | None,
        urls: Sequence[str],
    ) -> None:
        result = await fetch_image(s3, http, key, urls)
        if result is None:
            return
        data, kind = result
        # 動態的 PNG 會被偵測成 apng，瀏覽器一樣當作 PNG 播放
        extension = {"apng": "png"}.get(kind.extension, kind.extension)
        mime = "image/png" if kind.extension == "apng" else kind.mime
        self.images[css_class] = Image(css_class, f"{path}.{extension}", mime, data)

    def image_css(self) -> str:
        rules = []
        for image in self.images.values():
            if self.as_zip:
                url = quote(image.path)
            else:
                url = f"data:{image.content_type};base64,{base64.b64encode(image.data).decode()}"
            rules.append(f'.{image.css_class}{{background-image:url("{url}")}}')
        return "\n".join(rules)

    # ---- 給 markdown 的 callback ----

    def user_name(self, user_id: int) -> str | None:
        user = self.transcript.users.get(user_id)
        return display_name(user) if user is not None else None

    def emoji(self, emoji_id: int, name: str, animated: bool) -> str:
        label = escape(f":{name}:")
        css_class = f"emoji-{emoji_id}"
        if css_class not in self.images:
            return str(label)
        return (
            f'<span class="md-emoji {css_class}" role="img" aria-label="{label}" '
            f'title="{label}"></span>'
        )

    def markdown(self, text: str) -> Markup:
        env = markdown.Env(tz=self.tz, now=self.now, emoji=self.emoji, user_name=self.user_name)
        return Markup(markdown.render(text, env))

    # ---- 範本用的資料 ----

    def time(self, value: datetime) -> dict[str, str]:
        """跟網頁的 formatDateTime 一樣是年月日加時分，滑鼠移上去顯示完整的時間。"""
        local = value.astimezone(self.tz)
        now = self.now.astimezone(self.tz)
        return {
            "text": f"{local:%Y/%m/%d %H:%M}",
            "iso": local.isoformat(),
            "title": markdown.format_timestamp(local, "F", now),
        }

    def person(self, user_id: int) -> dict[str, str]:
        return {
            "name": display_name(self.transcript.users.get(user_id)),
            "avatar_class": f"av-{user_id}",
        }

    def author(self, user_id: int) -> dict[str, Any]:
        user = self.transcript.users.get(user_id)
        badge = None
        if user_id == self.transcript.ticket.opener_id:
            badge = {"label": "開單者", "color": "neutral"}
        elif user is not None and user.role_name:
            badge = {"label": user.role_name, "color": "primary"}
        return {
            **self.person(user_id),
            "color": f"#{user.role_color:06x}" if user is not None and user.role_color else None,
            "badge": badge,
        }

    def category_emoji(self) -> Markup | None:
        emoji = self.transcript.category.emoji if self.transcript.category else None
        if not emoji:
            return None
        match = markdown.CUSTOM_EMOJI.fullmatch(emoji)
        if match is None:
            return Markup(escape(emoji))
        return Markup(self.emoji(int(match[3]), match[2], bool(match[1])))

    def attachment(self, attachment: TicketAttachment) -> dict[str, Any]:
        item: dict[str, Any] = {
            "id": attachment.id,
            "name": attachment.filename,
            "size": format_size(attachment.size),
            "removed": attachment.removed_at is not None,
            "pending": attachment.status is AttachmentStatus.PENDING,
        }
        if attachment.status is not AttachmentStatus.DONE or attachment.storage_key is None:
            item["kind"] = "unavailable"
            item["status_text"] = ATTACHMENT_TEXT.get(attachment.status, "無法保存")
            return item
        content_type = attachment.content_type or ""
        item["href"] = quote(attachment_path(attachment))
        item["kind"] = next(
            (kind for kind in ("image", "video", "audio") if content_type.startswith(f"{kind}/")),
            "file",
        )
        return item

    def message(self, message: TicketMessage) -> dict[str, Any]:
        deleted = message.deleted_at is not None
        visible = self.staff or not deleted
        stickers = []
        for sticker in (message.stickers or []) if visible else []:
            css_class: str | None = f"sticker-{sticker['id']}"
            if css_class not in self.images:
                css_class = None
            stickers.append({"name": sticker["name"], "css_class": css_class})
        return {
            "id": message.id,
            "author": self.author(message.author_id),
            "source": "globe" if message.source.value == "web" else "discord",
            "created_at": self.time(message.created_at),
            "edited_at": (
                self.time(message.edited_at) if message.edited_at and not deleted else None
            ),
            "deleted": deleted,
            "content": self.markdown(message.content) if visible and message.content else None,
            "jumbo": markdown.is_jumbo_emoji(message.content),
            "stickers": stickers,
            "attachments": [
                self.attachment(attachment)
                for attachment in self.transcript.attachments.get(message.id, [])
            ],
            "delivery_failed": message.delivery_status is DeliveryStatus.FAILED,
            "revisions": [
                {
                    "created_at": self.time(revision.created_at),
                    "replaced_at": self.time(revision.replaced_at),
                    "content": self.markdown(revision.content),
                }
                for revision in self.transcript.revisions.get(message.id, [])
            ],
        }

    def opening(self) -> dict[str, Any]:
        """開單內容，顯示成第一則訊息。"""
        ticket = self.transcript.ticket
        return {
            "id": "opening",
            "author": self.author(ticket.opener_id),
            "source": "globe" if ticket.source.value == "web" else "discord",
            "created_at": self.time(ticket.created_at),
            "edited_at": None,
            "deleted": False,
            "content": self.markdown(ticket.content),
            "jumbo": markdown.is_jumbo_emoji(ticket.content),
            "stickers": [],
            "attachments": [],
            "delivery_failed": False,
            "revisions": [],
        }

    def page(self) -> str:
        ticket = self.transcript.ticket
        status = ticket.status.value
        if ticket.status is TicketStatus.OPEN and ticket.claimed_by_id is not None:
            status = "claimed"
        category = self.transcript.category
        return templates.get_template("export.html.j2").render(
            as_zip=self.as_zip,
            transcript_name=TRANSCRIPT,
            staff=self.staff,
            css=Markup(f"{CSS}\n{self.image_css()}"),
            ticket={
                "id": ticket.id,
                "subject": ticket.subject,
                "status": STATUS[status],
                "category_name": category.name if category else "（已刪除的分類）",
                "category_emoji": self.category_emoji(),
            },
            opening=self.opening(),
            messages=[self.message(message) for message in self.transcript.messages],
            info={
                "opener": self.person(ticket.opener_id),
                "claimer": self.person(ticket.claimed_by_id) if ticket.claimed_by_id else None,
                "members": [self.person(member.user_id) for member in self.transcript.members],
                "created_at": self.time(ticket.created_at),
                "last_activity": self.time(ticket.last_message_at),
                "closed_at": self.time(ticket.closed_at) if ticket.closed_at else None,
                "closed_by": (
                    display_name(self.transcript.users.get(ticket.closed_by_id))
                    if ticket.closed_by_id is not None
                    else "系統"
                ),
                "close_reason": ticket.close_reason,
                "rating": ticket.rating,
                "feedback": ticket.feedback,
            },
            exported_at=self.time(self.now),
        )


class _Sink:
    """zipfile 寫入的資料先放在這裡，再分塊上傳。不能 seek，zipfile 會改用 data descriptor。"""

    def __init__(self) -> None:
        self.buffer = bytearray()

    def write(self, data: bytes) -> int:
        self.buffer.extend(data)
        return len(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass

    def take(self) -> bytes:
        data = bytes(self.buffer)
        self.buffer.clear()
        return data


def zip_info(path: str, when: datetime, compress_type: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(path, date_time=when.timetuple()[:6])
    info.compress_type = compress_type
    # 一般檔案的權限（rw-r--r--），解壓縮後才不會沒有讀取權限
    info.external_attr = 0o644 << 16
    return info


async def zip_chunks(
    s3: S3Client,
    page: str,
    images: Sequence[Image],
    files: Sequence[TicketAttachment],
    when: datetime,
) -> AsyncIterator[bytes]:
    """一邊從 S3 讀附件一邊產生 ZIP，不會把整個檔案讀進記憶體。

    圖片、影片本來就壓縮過，不再壓縮（ZIP_STORED）；HTML 用 deflate。
    """
    sink = _Sink()
    with zipfile.ZipFile(sink, "w") as archive:
        archive.writestr(zip_info(TRANSCRIPT, when, zipfile.ZIP_DEFLATED), page)
        yield sink.take()
        for image in images:
            archive.writestr(zip_info(image.path, when, zipfile.ZIP_STORED), image.data)
            yield sink.take()
        for attachment in files:
            assert attachment.storage_key is not None
            info = zip_info(attachment_path(attachment), when, zipfile.ZIP_STORED)
            response = await s3.get_object(
                Bucket=get_settings().s3_bucket, Key=attachment.storage_key
            )
            # 超過 2 GiB 要用 Zip64；附件上限是 1 GB，保險起見還是依照大小判斷
            with archive.open(info, "w", force_zip64=attachment.size > 2**30) as dest:
                async with response["Body"] as body:
                    while chunk := await body.read(CHUNK_SIZE):
                        dest.write(chunk)
                        if len(sink.buffer) >= CHUNK_SIZE:
                            yield sink.take()
            yield sink.take()
    yield sink.take()


async def export_ticket(
    db: AsyncSession,
    s3: S3Client,
    http: aiohttp.ClientSession,
    export_id: int,
    *,
    last_attempt: bool,
) -> None:
    """產生匯出的檔案並存到 S3。冪等：不是 pending 的直接略過。"""
    lock = get_redis().lock(
        f"ticket-bot:lock:export:{export_id}", timeout=LOCK_TIMEOUT, blocking=False
    )
    if not await lock.acquire():
        raise ExportBusy(export_id)
    try:
        export = await db.get(TicketExport, export_id)
        if export is None or export.status is not ExportStatus.PENDING:
            return
        ticket = await db.get_one(Ticket, export.ticket_id)
        transcript = await load(db, ticket, staff=export.staff)
        # 讀完就結束交易，產生、上傳的期間才不會佔著資料庫
        await db.commit()
        pending = [
            attachment
            for attachment in transcript.all_attachments()
            if attachment.status is AttachmentStatus.PENDING
        ]
        if pending and not last_attempt:
            # 最後一次就不等了，沒處理完的附件只顯示檔名
            raise ExportNotReady(export_id)

        files = transcript.files()
        now = utcnow()
        tz = timezone(timedelta(minutes=export.utc_offset))
        renderer = Renderer(transcript, staff=export.staff, tz=tz, now=now, as_zip=bool(files))
        await renderer.load_images(s3, http)
        page = renderer.page()
        export_format = "zip" if files else "html"
        key = f"exports/{export.id}/ticket-{ticket.id}.{export_format}"
        if files:
            size = await upload_stream(
                s3,
                key,
                zip_chunks(s3, page, list(renderer.images.values()), files, now.astimezone(tz)),
                content_type="application/zip",
            )
        else:
            body = page.encode()
            await s3.put_object(
                Bucket=get_settings().s3_bucket,
                Key=key,
                Body=body,
                ContentType="text/html; charset=utf-8",
            )
            size = len(body)

        export.format = export_format
        export.storage_key = key
        export.size = size
        export.status = ExportStatus.DONE
        export.finished_at = utcnow()
        await db.commit()
    finally:
        with contextlib.suppress(LockError):
            await lock.release()


async def give_up(db: AsyncSession, export_id: int) -> None:
    """最後一次重試也失敗。"""
    export = await db.get(TicketExport, export_id)
    if export is not None and export.status is ExportStatus.PENDING:
        export.status = ExportStatus.FAILED
        export.finished_at = utcnow()
        await db.commit()


async def download_url(s3: S3Client, export: TicketExport) -> str | None:
    if export.status is not ExportStatus.DONE or export.storage_key is None:
        return None
    filename = f"ticket-{export.ticket_id}.{export.format}"
    content_type = "application/zip" if export.format == "zip" else "text/html; charset=utf-8"
    return await s3.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": get_settings().s3_bucket,
            "Key": export.storage_key,
            "ResponseContentType": content_type,
            # 一律下載，不在瀏覽器裡直接打開
            "ResponseContentDisposition": content_disposition(filename, inline=False),
        },
        ExpiresIn=get_settings().s3_presign_ttl,
    )


async def delete_expired(db: AsyncSession, s3: S3Client) -> int:
    """刪除超過 EXPORT_TTL 的匯出（S3 上的檔案與資料），回傳刪除了幾個。"""
    rows = list(
        await db.scalars(
            select(TicketExport).where(TicketExport.created_at < utcnow() - EXPORT_TTL)
        )
    )
    for export in rows:
        if export.storage_key is not None:
            await s3.delete_object(Bucket=get_settings().s3_bucket, Key=export.storage_key)
        await db.delete(export)
        await db.commit()
    return len(rows)
