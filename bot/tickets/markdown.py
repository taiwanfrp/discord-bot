"""Discord 的 Markdown 轉成 HTML（匯出用，docs/tickets.md 8.8）。

規則跟網頁的 app/utils/discordMarkdown.ts 一樣，網頁與匯出的檔案顯示結果才會一致。
"""

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timezone
from typing import Any

from markdown_it import MarkdownIt
from markdown_it.common.utils import escapeHtml
from markdown_it.renderer import RendererHTML
from markdown_it.rules_block import StateBlock
from markdown_it.rules_core import StateCore
from markdown_it.rules_inline import StateInline
from markdown_it.token import Token
from markdown_it.utils import OptionsDict

from bot.storage.media import EMOJI_PATTERN


@dataclass
class Env:
    """render 時傳入，放在 markdown-it 的 env（只接受 dict）裡傳給 render 規則。"""

    # 時間戳依照這個時區顯示（匯出時網頁送來的 UTC 時差）
    tz: timezone
    # 相對時間（<t:...:R>）以這個時間為準
    now: datetime
    # 自訂表情的 HTML：(id, 名稱, 是否動態) → HTML
    emoji: Callable[[int, str, bool], str]
    # <@id> 顯示的名稱；None 表示不是這張單裡的人
    user_name: Callable[[int], str | None]


# html=False 會跳脫原始 HTML
md = MarkdownIt("js-default", {"html": False, "breaks": True, "linkify": True})
# Discord 只會自動連結有 http(s):// 的網址
assert md.linkify is not None
md.linkify.set({"fuzzy_link": False, "fuzzy_email": False})
# 停用 Discord 不支援的語法：表格、縮排程式碼、分隔線、底線式標題、參考連結、圖片
md.disable(["table", "code", "hr", "lheading", "reference", "image"])


def _heading_levels(state: StateCore) -> None:
    """Discord 只支援 # ~ ###，更多層的 # 照原樣顯示。"""
    tokens = state.tokens
    for i, open_token in enumerate(tokens):
        if open_token.type != "heading_open" or int(open_token.tag[1:]) <= 3:
            continue
        inline, close = tokens[i + 1], tokens[i + 2]
        inline.content = f"{open_token.markup} {inline.content}"
        open_token.type, open_token.tag = "paragraph_open", "p"
        close.type, close.tag = "paragraph_close", "p"


md.core.ruler.after("block", "discord_heading_levels", _heading_levels)


def _subtext(state: StateBlock, start_line: int, _end_line: int, silent: bool) -> bool:
    """-# 小字"""
    start = state.bMarks[start_line] + state.tShift[start_line]
    line = state.src[start : state.eMarks[start_line]]
    if not line.startswith("-# "):
        return False
    if silent:
        return True
    open_token = state.push("paragraph_open", "p", 1)
    open_token.attrSet("class", "md-subtext")
    open_token.map = [start_line, start_line + 1]
    inline = state.push("inline", "", 0)
    inline.content = line[3:].strip()
    inline.map = [start_line, start_line + 1]
    inline.children = []
    state.push("paragraph_close", "p", -1)
    state.line = start_line + 1
    return True


md.block.ruler.before("paragraph", "discord_subtext", _subtext, {"alt": ["paragraph"]})

# 內建的 text 規則不會在 | 停下來，||防雷|| 會被當成一般文字；沿用內建的停止字元，再加上 |
_TEXT_TERMINATORS = frozenset("\n!#$%&*+-:<=@[\\]^_`{}~|")


def _text(state: StateInline, silent: bool) -> bool:
    pos = state.pos
    while pos < state.posMax and state.src[pos] not in _TEXT_TERMINATORS:
        pos += 1
    if pos == state.pos:
        return False
    if not silent:
        state.pending += state.src[state.pos : pos]
    state.pos = pos
    return True


md.inline.ruler.at("text", _text)


def _spoiler(state: StateInline, silent: bool) -> bool:
    """||防雷||"""
    start = state.pos
    if not state.src.startswith("||", start):
        return False
    end = state.src.find("||", start + 2)
    if end == -1 or end > state.posMax or end == start + 2:
        return False
    if not silent:
        spoiler = state.push("spoiler_open", "span", 1)
        spoiler.attrSet("class", "md-spoiler")
        # 匯出的 HTML 沒有 JavaScript，用 Tab 選到時顯示（CSS 的 :focus）
        spoiler.attrSet("tabindex", "0")
        old_pos_max = state.posMax
        state.pos = start + 2
        state.posMax = end
        state.md.inline.tokenize(state)
        state.posMax = old_pos_max
        state.push("spoiler_close", "span", -1)
    state.pos = end + 2
    return True


md.inline.ruler.before("emphasis", "discord_spoiler", _spoiler)

TIMESTAMP_PATTERN = re.compile(r"<t:(-?\d{1,13})(?::([tTdDfFR]))?>")
WEEKDAYS = "一二三四五六日"


def format_timestamp(value: datetime, style: str, now: datetime) -> str:
    """跟 Discord 的時間戳格式一樣（台灣的寫法）。value 與 now 要是同一個時區。"""
    time = f"{value:%H:%M}"
    date = f"{value.year}年{value.month}月{value.day}日"
    match style:
        case "t":
            return time
        case "T":
            return f"{value:%H:%M:%S}"
        case "d":
            return f"{value:%Y/%m/%d}"
        case "D":
            return date
        case "F":
            return f"{date} 星期{WEEKDAYS[value.weekday()]} {time}"
        case "R":
            return relative_time(value, now)
    return f"{date} {time}"


RELATIVE_UNITS = (
    ("年", 365 * 86400),
    ("個月", 30 * 86400),
    ("天", 86400),
    ("小時", 3600),
    ("分鐘", 60),
    ("秒", 1),
)


def relative_time(value: datetime, now: datetime) -> str:
    seconds = (value - now).total_seconds()
    unit, size = next(
        ((unit, size) for unit, size in RELATIVE_UNITS if abs(seconds) >= size), ("秒", 1)
    )
    count = round(abs(seconds) / size)
    return f"{count} {unit}{'後' if seconds > 0 else '前'}"


def _timestamp(state: StateInline, silent: bool) -> bool:
    match = TIMESTAMP_PATTERN.match(state.src, state.pos, state.posMax)
    if match is None:
        return False
    try:
        value = datetime.fromtimestamp(int(match[1]), UTC)
    except (OverflowError, OSError, ValueError):
        return False
    if not silent:
        token = state.push("discord_timestamp", "time", 0)
        token.meta = {"value": value, "style": match[2] or "f"}
    state.pos = match.end()
    return True


md.inline.ruler.before("autolink", "discord_timestamp", _timestamp)


def _render_timestamp(
    self: RendererHTML, tokens: Sequence[Token], idx: int, options: OptionsDict, env: dict[str, Env]
) -> str:
    value: datetime = tokens[idx].meta["value"].astimezone(env["export"].tz)
    now = env["export"].now.astimezone(env["export"].tz)
    full = escapeHtml(format_timestamp(value, "F", now))
    text = escapeHtml(format_timestamp(value, tokens[idx].meta["style"], now))
    return f'<time class="md-timestamp" datetime="{value.isoformat()}" title="{full}">{text}</time>'


md.add_render_rule("discord_timestamp", _render_timestamp)

CUSTOM_EMOJI = re.compile(r"<(a?):(\w{2,32}):(\d{15,21})>")


def _emoji(state: StateInline, silent: bool) -> bool:
    match = CUSTOM_EMOJI.match(state.src, state.pos, state.posMax)
    if match is None:
        return False
    if not silent:
        token = state.push("discord_emoji", "span", 0)
        token.meta = {"animated": bool(match[1]), "name": match[2], "id": int(match[3])}
    state.pos = match.end()
    return True


md.inline.ruler.before("autolink", "discord_emoji", _emoji)


def _render_emoji(
    self: RendererHTML, tokens: Sequence[Token], idx: int, options: OptionsDict, env: dict[str, Env]
) -> str:
    meta = tokens[idx].meta
    return env["export"].emoji(meta["id"], meta["name"], meta["animated"])


md.add_render_rule("discord_emoji", _render_emoji)

USER_MENTION = re.compile(r"<@!?(\d{15,21})>")


def _mention(state: StateInline, silent: bool) -> bool:
    """<@id>：網頁上照原樣顯示；匯出時有快照的人換成名稱。"""
    match = USER_MENTION.match(state.src, state.pos, state.posMax)
    if match is None:
        return False
    if not silent:
        state.push("discord_mention", "span", 0).meta = {"id": int(match[1]), "raw": match[0]}
    state.pos = match.end()
    return True


md.inline.ruler.before("autolink", "discord_mention", _mention)


def _render_mention(
    self: RendererHTML, tokens: Sequence[Token], idx: int, options: OptionsDict, env: dict[str, Env]
) -> str:
    meta = tokens[idx].meta
    name = env["export"].user_name(meta["id"])
    if name is None:
        return escapeHtml(meta["raw"])
    return f'<span class="md-mention">@{escapeHtml(name)}</span>'


md.add_render_rule("discord_mention", _render_mention)


def _strong(tag: str) -> Callable[..., str]:
    """Discord 的 __文字__ 是底線，**文字** 才是粗體。"""

    def render(
        self: RendererHTML, tokens: Sequence[Token], idx: int, options: OptionsDict, env: Any
    ) -> str:
        if tokens[idx].markup == "__":
            return tag
        return self.renderToken(tokens, idx, options, env)

    return render


md.add_render_rule("strong_open", _strong("<u>"))
md.add_render_rule("strong_close", _strong("</u>"))


def _link_open(
    self: RendererHTML, tokens: Sequence[Token], idx: int, options: OptionsDict, env: Any
) -> str:
    tokens[idx].attrSet("target", "_blank")
    tokens[idx].attrSet("rel", "noopener noreferrer")
    return self.renderToken(tokens, idx, options, env)


md.add_render_rule("link_open", _link_open)


def normalize_quotes(text: str) -> str:
    """把 Discord 的引用規則轉成標準 Markdown。

    - >>> 之後的所有內容都是引用
    - > 只引用該行，下一行不是 > 開頭就結束引用（標準 Markdown 會延續到下一行）
    """
    result: list[str] = []
    in_multi_quote = False
    for line in text.split("\n"):
        if in_multi_quote:
            result.append(f"> {line}")
            continue
        if line.startswith(">>> "):
            in_multi_quote = True
            result.append(f"> {line[4:]}")
            continue
        if result and result[-1].startswith("> ") and not line.startswith("> ") and line.strip():
            result.append("")
        result.append(line)
    return "\n".join(result)


def render(text: str, env: Env) -> str:
    html: str = md.render(normalize_quotes(text), {"export": env})
    return html


# 跟 Discord 一樣，只有表情（最多 30 個）的訊息顯示大的表情。
# Python 的 re 沒有 \p{Extended_Pictographic}，用 Unicode 表情所在的區段判斷
_EMOJI_JOINERS = frozenset("‍️")


def _is_emoji_char(char: str) -> bool:
    code = ord(char)
    return code >= 0x1F000 or 0x2600 <= code <= 0x27BF or 0x2B00 <= code <= 0x2BFF


def is_jumbo_emoji(text: str) -> bool:
    count = len(EMOJI_PATTERN.findall(text))
    rest = EMOJI_PATTERN.sub("", text)
    for char in rest:
        if char.isspace() or char in _EMOJI_JOINERS:
            continue
        if not _is_emoji_char(char):
            return False
        # 膚色（U+1F3FB ~ 1F3FF）跟前面的表情是同一個；國旗是兩個區域指示符號
        if not 0x1F3FB <= ord(char) <= 0x1F3FF:
            count += 1
    count -= sum(1 for char in rest if 0x1F1E6 <= ord(char) <= 0x1F1FF) // 2
    return 0 < count <= 30
