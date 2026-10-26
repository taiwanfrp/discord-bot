import sys

from bot.core.bot import Bot
from bot.core.config import get_settings

if __name__ == "__main__":
    bot = Bot()
    bot.run(get_settings().discord_token.get_secret_value(), root_logger=True)
    if bot.lost_instance_lock:
        # 另一個 bot 拿走了鎖（docs/tickets.md 11.3），用非 0 的狀態碼表示不正常結束
        sys.exit(1)
