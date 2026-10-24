import pkgutil

import pytest

from bot import cogs
from bot.core.bot import Bot

COG_MODULES = [m.name for m in pkgutil.iter_modules(cogs.__path__, f"{cogs.__name__}.")]


@pytest.mark.parametrize("name", COG_MODULES)
async def test_load_cog(name: str) -> None:
    bot = Bot()
    await bot.load_extension(name)
    assert len(bot.cogs) == 1
    await bot.close()
