from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    discord_token: SecretStr
    database_url: str
    redis_url: str
    encryption_keys: SecretStr


@lru_cache
def get_settings() -> Settings:
    return Settings()
