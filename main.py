from bot.core.bot import Bot
from bot.core.config import get_settings

if __name__ == "__main__":
    Bot().run(get_settings().discord_token.get_secret_value(), root_logger=True)
