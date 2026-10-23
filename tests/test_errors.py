from discord import app_commands

from bot.core.tree import UserFacingError, error_message


def test_user_facing_error() -> None:
    assert error_message(UserFacingError("找不到這個節點")) == "找不到這個節點"


def test_missing_permissions() -> None:
    error = app_commands.MissingPermissions(["manage_roles"])
    assert error_message(error) == "你缺少權限：manage_roles"


def test_no_private_message() -> None:
    assert error_message(app_commands.NoPrivateMessage()) == "這個指令只能在伺服器中使用"


def test_unexpected_error() -> None:
    assert error_message(app_commands.AppCommandError("boom")) is None
