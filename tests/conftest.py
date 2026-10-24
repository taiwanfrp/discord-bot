import os

from cryptography.fernet import Fernet

# 測試不使用 .env 裡的設定
os.environ["DISCORD_TOKEN"] = "test-token"
os.environ["DATABASE_URL"] = "sqlite+aiosqlite://"
# 測試一律用 fakeredis；指到沒有服務的 port，不小心連到真的 Redis 時會直接失敗
os.environ["REDIS_URL"] = "redis://127.0.0.1:1"
os.environ["SESSION_REDIS_URL"] = "redis://127.0.0.1:1"
os.environ["GUILD_ID"] = "1"
os.environ["API_CORS_ORIGINS"] = "https://web.test"
os.environ["ENCRYPTION_KEYS"] = Fernet.generate_key().decode()
