import os

from cryptography.fernet import Fernet

# 測試不使用 .env 裡的設定
os.environ["DISCORD_TOKEN"] = "test-token"
os.environ["DATABASE_URL"] = "sqlite+aiosqlite://"
os.environ["ENCRYPTION_KEYS"] = Fernet.generate_key().decode()
