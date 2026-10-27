from functools import lru_cache
from typing import Annotated

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    # 空白的環境變數當作沒有設定，使用預設值（compose.prod.yaml 的選填變數沒填時是空白）
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    discord_token: SecretStr
    guild_id: int
    database_url: str
    redis_url: str
    encryption_keys: SecretStr

    # S3 相容儲存（docs/tickets.md 8.4）；endpoint 是 None 時使用 AWS
    s3_endpoint: str | None = None
    s3_region: str = "us-east-1"
    s3_access_key_id: str
    s3_secret_access_key: SecretStr
    s3_bucket: str
    # presigned URL 的有效期限（秒）
    s3_presign_ttl: int = 600
    # bot 對外的網址，用來組出分享連結（docs/tickets.md 8.4.1），例如 https://bot.example.com
    public_base_url: str
    # 網頁上紀錄頁面的網址，{id} 會換成客服單 id，例如 https://example.com/tickets/{id}
    web_ticket_url: str

    api_host: str = "127.0.0.1"
    api_port: int = 8000
    # 逗號分隔
    api_cors_origins: Annotated[list[str], NoDecode] = []
    forwarded_allow_ips: str = "127.0.0.1"
    session_redis_url: str
    auth_cookie_name: str = "__Host-ticket_session"
    ticket_session_ttl: int = 3 * 24 * 60 * 60

    @field_validator("public_base_url")
    @classmethod
    def _strip_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("web_ticket_url")
    @classmethod
    def _require_id(cls, value: str) -> str:
        if "{id}" not in value:
            raise ValueError("WEB_TICKET_URL 要包含 {id}")
        return value

    @field_validator("api_cors_origins", mode="before")
    @classmethod
    def _split_comma(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
