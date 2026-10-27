import pytest

from bot.core.config import Settings


def test_empty_optional_variables_use_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "S3_ENDPOINT",
        "S3_REGION",
        "S3_PRESIGN_TTL",
        "AUTH_COOKIE_NAME",
        "TICKET_SESSION_TTL",
    ):
        monkeypatch.setenv(name, "")

    settings = Settings(_env_file=None)

    assert settings.s3_endpoint is None
    assert settings.s3_region == "us-east-1"
    assert settings.s3_presign_ttl == 600
    assert settings.auth_cookie_name == "__Host-ticket_session"
    assert settings.ticket_session_ttl == 3 * 24 * 60 * 60


def test_empty_required_variable_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GUILD_ID", "")

    with pytest.raises(ValueError, match="guild_id"):
        Settings(_env_file=None)
