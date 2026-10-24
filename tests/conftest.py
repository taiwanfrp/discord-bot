import os

from cryptography.fernet import Fernet

# 測試不使用 .env 裡的設定
os.environ["DISCORD_TOKEN"] = "test-token"
os.environ["DATABASE_URL"] = "sqlite+aiosqlite://"
os.environ["REDIS_URL"] = "redis://localhost:6379"
os.environ["SESSION_REDIS_URL"] = "redis://localhost:6379"
os.environ["GUILD_ID"] = "1"
os.environ["API_CORS_ORIGINS"] = "https://web.test"
os.environ["ENCRYPTION_KEYS"] = Fernet.generate_key().decode()
