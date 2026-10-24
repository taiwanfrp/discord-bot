import re

# 頻道名稱可以使用的變數（docs/tickets.md 6.2）。只替換白名單內的變數，
# 不可以用 str.format()，因為它可以讀取物件的屬性
_VARIABLE = re.compile(r"\{(?:(id)(?::(\d{1,2}))?|(category)|(opener))\}")
MAX_LENGTH = 100


def validate_template(template: str) -> None:
    rest = _VARIABLE.sub("", template)
    if "{" in rest or "}" in rest:
        raise ValueError("只能使用 {id}、{id:位數}、{category}、{opener} 這幾個變數")
    if not render_channel_name(template, ticket_id=1, category="分類", opener="user").strip():
        raise ValueError("頻道名稱不能是空的")


def render_channel_name(template: str, *, ticket_id: int, category: str, opener: str) -> str:
    def replace(match: re.Match[str]) -> str:
        if match.group(1):
            width = match.group(2)
            return str(ticket_id).zfill(int(width)) if width else str(ticket_id)
        if match.group(3):
            return category
        return opener

    return _VARIABLE.sub(replace, template)[:MAX_LENGTH]
