from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

import uvicorn
from discord.ext import commands
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from bot.api.csrf import CSRFMiddleware
from bot.api.deps import BotDep, UserCache
from bot.api.routes import (
    attachments,
    auth,
    categories,
    events,
    guild,
    media,
    messages,
    panel,
    permission_roles,
    share,
    tickets,
)
from bot.api.routes import settings as settings_routes
from bot.api.sse import EventHub
from bot.core.config import get_settings

if TYPE_CHECKING:
    from types_aiobotocore_s3 import S3Client


def create_app(bot: commands.Bot, event_hub: EventHub, s3: S3Client) -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="TaiwanFRP Ticket Bot")
    app.state.bot = bot
    app.state.event_hub = event_hub
    app.state.s3 = s3
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
    # 最外層：壓縮回應（訊息的 JSON 可能有幾百 KB）。SSE（text/event-stream）預設不壓縮，
    # 事件才不會被緩衝；小於 500 bytes 的也不壓縮。等級 9 只比 6 小 1～2%，時間卻多了快一倍
    app.add_middleware(GZipMiddleware, compresslevel=6)

    for module in (
        auth,
        guild,
        settings_routes,
        categories,
        permission_roles,
        panel,
        tickets,
        messages,
        attachments,
        share,
        media,
        events,
    ):
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


def create_server(bot: commands.Bot, event_hub: EventHub, s3: S3Client) -> Server:
    settings = get_settings()
    config = uvicorn.Config(
        create_app(bot, event_hub, s3),
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
