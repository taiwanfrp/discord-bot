from functools import lru_cache
from typing import Annotated

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    discord_token: SecretStr
    guild_id: int
    database_url: str
    redis_url: str
    encryption_keys: SecretStr

    api_host: str = "127.0.0.1"
    api_port: int = 8000
    # 逗號分隔
    api_cors_origins: Annotated[list[str], NoDecode] = []
    forwarded_allow_ips: str = "127.0.0.1"
    session_redis_url: str
    auth_cookie_name: str = "__Host-ticket_session"
    ticket_session_ttl: int = 3 * 24 * 60 * 60

    @field_validator("api_cors_origins", mode="before")
    @classmethod
    def _split_comma(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
