import io
import zipfile

import pytest

from bot.storage.files import (
    DEFAULT_FILE_TYPES,
    MAGIC_BYTES,
    attachment_key,
    avatar_key,
    check_magic,
    content_disposition,
    detect_content_type,
    is_allowed_extension,
    is_inline,
    safe_filename,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 300
EXE = b"MZ" + b"\x00" * 300


def zip_file(*names: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in names:
            archive.writestr(name, "x" * 500)
    return buffer.getvalue()


def test_magic_bytes() -> None:
    assert check_magic(PNG, "a.png", DEFAULT_FILE_TYPES) is None
    # 執行檔改了副檔名也擋得下來
    assert check_magic(EXE, "a.png", DEFAULT_FILE_TYPES) == "exe"
    # 純文字沒有 magic bytes，只看副檔名
    assert check_magic(b"hello, world\n", "a.txt", DEFAULT_FILE_TYPES) is None


def test_magic_bytes_aliases() -> None:
    # 動態 PNG 會被偵測成 apng
    apng = b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR" + b"\x00" * 17 + b"\x00\x00\x00\x08acTL"
    assert check_magic(apng + b"\x00" * 300, "a.png", frozenset({"png"})) is None


def test_magic_bytes_zip_family() -> None:
    docx = zip_file("[Content_Types].xml", "_rels/.rels", "word/document.xml")
    assert check_magic(docx[:MAGIC_BYTES], "a.docx", frozenset({"docx"})) is None
    # 偵測成 zip 的 docx（例如檔頭比較長）也接受
    plain_zip = zip_file("a.txt")
    assert check_magic(plain_zip, "a.docx", frozenset({"docx"})) is None
    # 但副檔名不在同一組就不接受
    assert check_magic(plain_zip, "a.pdf", frozenset({"pdf"})) == "zip"


@pytest.mark.parametrize(
    ("filename", "allowed"),
    [("a.PNG", True), ("photo.jpeg", True), ("a.exe", False), ("noext", False), ("a.tar.gz", True)],
)
def test_extension(filename: str, allowed: bool) -> None:
    assert is_allowed_extension(filename, DEFAULT_FILE_TYPES) is allowed


def test_safe_filename() -> None:
    assert safe_filename("../../etc/passwd") == "_.._etc_passwd"
    assert safe_filename("a\x00b\nc.png") == "a_b_c.png"
    assert safe_filename("...") == "file"
    long = safe_filename("名" * 300 + ".png")
    assert len(long) == 100 and long.endswith(".png")


def test_keys() -> None:
    assert attachment_key(1, 2, "a/b.png") == "tickets/1/2/a_b.png"
    assert avatar_key(5, "abc") == "avatars/5/abc.webp"
    assert avatar_key(5, "a_abc") == "avatars/5/a_abc.gif"


def test_content_type() -> None:
    assert detect_content_type(PNG) == "image/png"
    # 偵測不到（例如純文字）時一律下載
    assert detect_content_type(b"hello") == "application/octet-stream"
    assert is_inline("image/png") and is_inline("video/mp4")
    assert not is_inline("application/pdf")
    assert not is_inline("image/svg+xml")
    assert not is_inline(None)


def test_content_disposition() -> None:
    assert content_disposition("a.pdf", inline=False) == (
        "attachment; filename=\"a.pdf\"; filename*=UTF-8''a.pdf"
    )
    # 非 ASCII 與引號只出現在 filename* 裡（百分比編碼）
    assert content_disposition('圖 "1".png', inline=True) == (
        "inline; filename=\"_ _1_.png\"; filename*=UTF-8''%E5%9C%96%20%221%22.png"
    )
