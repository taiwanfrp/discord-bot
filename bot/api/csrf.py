from collections.abc import Collection

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class CSRFMiddleware:
    """會修改資料的請求：Origin 要在白名單內，而且只接受 JSON（docs/auth.md 第 7 節）。

    只接受 JSON 會強制瀏覽器先送 preflight，讓 CORS 白名單發揮作用。
    """

    def __init__(self, app: ASGIApp, allowed_origins: Collection[str]) -> None:
        self.app = app
        self.allowed_origins = set(allowed_origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] not in SAFE_METHODS:
            headers = Headers(scope=scope)
            content_type = headers.get("content-type", "").split(";")[0].strip().lower()
            if headers.get("origin") not in self.allowed_origins:
                response = JSONResponse({"detail": "Origin not allowed"}, status_code=403)
                await response(scope, receive, send)
                return
            if content_type != "application/json":
                response = JSONResponse({"detail": "Content-Type must be JSON"}, status_code=415)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
