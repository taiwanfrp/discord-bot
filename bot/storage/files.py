import re
import unicodedata
from urllib.parse import quote

import filetype

from bot.db.models import GuildSettings

# 預設允許的副檔名（docs/tickets.md 8.3）：圖片、影片、音訊、文件、壓縮檔
DEFAULT_FILE_TYPES = frozenset(
    {
        *("png", "jpg", "jpeg", "gif", "webp", "bmp", "heic", "avif"),
        *("mp4", "mov", "webm", "mkv", "avi"),
        *("mp3", "wav", "ogg", "m4a", "flac"),
        *("pdf", "txt", "log", "csv", "json", "md"),
        *("doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "odp"),
        *("zip", "7z", "rar", "tar", "gz"),
    }
)
# 副檔名或 filetype 偵測到的類型，跟允許清單的寫法不同時（動態 PNG 會被偵測成 apng）
_ALIASES = {"jpeg": "jpg", "apng": "png", "m4v": "mp4"}
# 同一種容器格式，filetype 不一定分得出來（例如 docx 可能被偵測成 zip）。
# 偵測到的類型跟副檔名在同一組，而且副檔名被允許時就接受
_FAMILIES = (
    frozenset({"zip", "docx", "xlsx", "pptx", "odt", "ods", "odp", "epub"}),
    frozenset({"doc", "xls", "ppt"}),
)
# filetype 最多看檔案開頭的這麼多 bytes；太短的話 docx 等會被偵測成 zip
MAGIC_BYTES = 8192
MAX_FILENAME_LENGTH = 100


def allowed_file_types(settings: GuildSettings) -> frozenset[str]:
    if settings.allowed_file_types is None:
        return DEFAULT_FILE_TYPES
    return frozenset(settings.allowed_file_types)


def extension(filename: str) -> str:
    _, dot, ext = filename.rpartition(".")
    return ext.lower() if dot else ""


def _is_allowed(ext: str, allowed: frozenset[str]) -> bool:
    return ext in allowed or _ALIASES.get(ext, ext) in allowed


def is_allowed_extension(filename: str, allowed: frozenset[str]) -> bool:
    return _is_allowed(extension(filename), allowed)


def check_magic(head: bytes, filename: str, allowed: frozenset[str]) -> str | None:
    """用檔案開頭的 magic bytes 判斷實際的類型。不允許的話回傳偵測到的類型。

    純文字（txt、csv 等）沒有 magic bytes，偵測不到就只依照副檔名。
    """
    kind = filetype.guess(head)
    if kind is None:
        return None
    detected = str(kind.extension)
    if _is_allowed(detected, allowed):
        return None
    declared = extension(filename)
    if _is_allowed(declared, allowed) and any(
        detected in family and declared in family for family in _FAMILIES
    ):
        return None
    return detected


def detect_content_type(head: bytes) -> str:
    """用 magic bytes 判斷的 MIME type，偵測不到（例如純文字）時是 application/octet-stream。

    網頁上傳的檔案下載時用它當作 Content-Type，不相信瀏覽器送來的類型。
    """
    mime = filetype.guess_mime(head)
    return str(mime) if mime else "application/octet-stream"


def is_inline(content_type: str | None) -> bool:
    """圖片、影片直接顯示（Discord 才會顯示預覽），其他檔案一律下載。

    SVG 裡面可以放 script，所以不直接顯示。
    """
    if content_type is None or content_type == "image/svg+xml":
        return False
    return content_type.startswith(("image/", "video/"))


def content_disposition(filename: str, *, inline: bool) -> str:
    """下載時還原原始檔名（RFC 6266）：filename* 是 UTF-8，filename 給不支援的程式用。"""
    fallback = re.sub(r'[^\x20-\x7e]|["\\]', "_", filename)
    kind = "inline" if inline else "attachment"
    return f"{kind}; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"


def safe_filename(filename: str) -> str:
    """S3 key 用的檔名：移除路徑分隔符號、控制字元，並限制長度（保留副檔名）。"""
    name = unicodedata.normalize("NFC", filename)
    name = re.sub(r"[\x00-\x1f\x7f/\\]", "_", name).strip(" .") or "file"
    if len(name) > MAX_FILENAME_LENGTH:
        ext = extension(name)
        keep = MAX_FILENAME_LENGTH - (len(ext) + 1 if ext else 0)
        name = name[:keep] + (f".{ext}" if ext else "")
    return name


def attachment_key(ticket_id: int, attachment_id: int, filename: str) -> str:
    return f"tickets/{ticket_id}/{attachment_id}/{safe_filename(filename)}"


def avatar_key(user_id: int, avatar_hash: str) -> str:
    # 動態頭像的 hash 以 a_ 開頭，存成 gif
    ext = "gif" if avatar_hash.startswith("a_") else "webp"
    return f"avatars/{user_id}/{avatar_hash}.{ext}"
