from datetime import UTC, datetime, timedelta, timezone

import pytest

from bot.tickets.markdown import Env, is_jumbo_emoji, render

TAIPEI = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 30, 0, 0, tzinfo=UTC)
USER = 123456789012345678


def env() -> Env:
    return Env(
        tz=TAIPEI,
        now=NOW,
        emoji=lambda emoji_id, name, animated: f"[{emoji_id}:{name}:{animated}]",
        user_name=lambda user_id: "小明" if user_id == USER else None,
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("**粗** __底線__ *斜* ~~刪~~", "<strong>粗</strong> <u>底線</u> <em>斜</em> <s>刪</s>"),
        (
            "||防雷 **in**||",
            '<span class="md-spoiler" tabindex="0">防雷 <strong>in</strong></span>',
        ),
        ("<script>x</script>", "&lt;script&gt;x&lt;/script&gt;"),
        ("-# 小字", '<p class="md-subtext">小字</p>'),
        ("#### 四層", "<p>#### 四層</p>"),
        ("<a:_Gura:983318658476552222>", "[983318658476552222:_Gura:True]"),
        (f"<@{USER}> <@!999999999999999999>", '<span class="md-mention">@小明</span> &lt;@!999'),
        ("`<:a_b:983318658476552222>`", "<code>&lt;:a_b:983318658476552222&gt;</code>"),
        ("www.example.com", "<p>www.example.com</p>"),
        ("https://a.test", '<a href="https://a.test" target="_blank" rel="noopener noreferrer">'),
        ("> 引用\n不是引用", "<blockquote>\n<p>引用</p>\n</blockquote>\n<p>不是引用</p>"),
        (">>> 全部\n都是", "<blockquote>\n<p>全部<br>\n都是</p>"),
    ],
)
def test_render(text: str, expected: str) -> None:
    assert expected in render(text, env())


def test_timestamps() -> None:
    # 2026-10-28 06:00 UTC = 台灣 14:00
    html = render("<t:1793167200:F> <t:1793167200:d> <t:1793167200:R> <t:1793167200>", env())
    assert ">2026年10月28日 星期三 14:00</time>" in html
    assert ">2026/10/28</time>" in html
    assert ">2 天前</time>" in html
    assert ">2026年10月28日 14:00</time>" in html
    assert 'datetime="2026-10-28T14:00:00+08:00"' in html


@pytest.mark.parametrize(
    ("text", "jumbo"),
    [
        ("<:_Gura:983318658476552222>", True),
        ("💩", True),
        ("🇹🇼", True),
        ("👍🏽 👨‍👩‍👧", True),
        ("hi 💩", False),
        ("💩" * 31, False),
        ("", False),
    ],
)
def test_jumbo_emoji(text: str, jumbo: bool) -> None:
    assert is_jumbo_emoji(text) is jumbo
