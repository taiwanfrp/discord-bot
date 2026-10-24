from collections.abc import Iterator
from contextlib import contextmanager

import uvicorn
from discord.ext import commands
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from bot.api.csrf import CSRFMiddleware
from bot.api.deps import BotDep, UserCache
from bot.api.routes import auth, categories, guild, panel, permission_roles, tickets
from bot.api.routes import settings as settings_routes
from bot.core.config import get_settings


def create_app(bot: commands.Bot) -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="TaiwanFRP Ticket Bot")
    app.state.bot = bot
    app.state.user_cache = UserCache()

    # 後加入的 middleware 在外層：CORS 先處理 preflight，再檢查 CSRF
    app.add_middleware(CSRFMiddleware, allowed_origins=settings.api_cors_origins)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.api_cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "Last-Event-ID"],
    )

    for module in (auth, guild, settings_routes, categories, permission_roles, panel, tickets):
        app.include_router(module.router)

    @app.get("/health")
    async def health(bot: BotDep) -> dict[str, bool]:
        return {"ok": True, "discord_ready": bot.is_ready()}

    return app


class Server(uvicorn.Server):
    """在 bot 的 event loop 裡執行的 uvicorn。"""

    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        # 訊號由 bot 處理，關閉時 bot 會設定 should_exit
        yield


def create_server(bot: commands.Bot) -> Server:
    settings = get_settings()
    config = uvicorn.Config(
        create_app(bot),
        host=settings.api_host,
        port=settings.api_port,
        # 只信任自己的 proxy 送來的 X-Forwarded-For（docs/auth.md 第 8 節）
        proxy_headers=True,
        forwarded_allow_ips=settings.forwarded_allow_ips,
        # 使用 bot 的 logging 設定
        log_config=None,
        # 資源由 bot 管理
        lifespan="off",
        server_header=False,
        # SSE 是長連線，關閉時最多等這麼久
        timeout_graceful_shutdown=10,
    )
    return Server(config)
