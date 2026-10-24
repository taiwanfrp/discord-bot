import pytest

from bot.tickets.channel_name import render_channel_name, validate_template


def test_render() -> None:
    name = render_channel_name(
        "{category}-{id:4}-{opener}-{id}", ticket_id=12, category="帳務", opener="bob"
    )
    assert name == "帳務-0012-bob-12"


def test_render_truncates() -> None:
    assert len(render_channel_name("x" * 99 + "{id}", ticket_id=123, category="", opener="")) == 100


@pytest.mark.parametrize("template", ["客服單-{id}", "{id:4}", "{category}-{opener}", "no-vars"])
def test_valid_templates(template: str) -> None:
    validate_template(template)


@pytest.mark.parametrize(
    "template", ["{name}", "{id.__class__}", "{0}", "{id:x}", "{", "}", "{category"]
)
def test_invalid_templates(template: str) -> None:
    with pytest.raises(ValueError):
        validate_template(template)
