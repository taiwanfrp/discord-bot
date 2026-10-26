import asyncio

import pytest
from starlette.types import Message, Scope

from tests.api.helpers import Env
from tests.api.test_ticket_actions import open_ticket


async def test_gzip(env: Env) -> None:
    ticket_id, _ = await open_ticket(env)
    assert (await env.post(f"/tickets/{ticket_id}/messages", {"content": "log " * 500})).is_success

    # httpx 預設會送 Accept-Encoding: gzip，並且自動解壓縮
    response = await env.client.get(f"/tickets/{ticket_id}/messages")
    assert response.headers["content-encoding"] == "gzip"
    assert response.json()["messages"][-1]["content"] == "log " * 500
    # 太小的回應不壓縮
    assert "content-encoding" not in (await env.client.get("/health")).headers


async def test_sse_not_compressed(env: Env) -> None:
    """SSE 被壓縮的話，事件會被緩衝住，瀏覽器收不到即時的事件。"""
    ticket_id, _ = await open_ticket(env)
    cookie = "; ".join(f"{name}={value}" for name, value in env.client.cookies.items())
    path = f"/tickets/{ticket_id}/events"
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"bot.test"),
            (b"accept-encoding", b"gzip"),
            (b"cookie", cookie.encode()),
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("bot.test", 443),
    }
    started: asyncio.Future[Message] = asyncio.get_running_loop().create_future()
    disconnected = asyncio.Event()

    async def receive() -> Message:
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.start" and not started.done():
            started.set_result(message)

    # SSE 不會結束，所以直接呼叫 ASGI app，只看回應的 header
    task = asyncio.create_task(env.app(scope, receive, send))
    try:
        # 被壓縮的話，要等到第一個事件或 heartbeat（15 秒）才會送出 header
        start = await asyncio.wait_for(started, 2)
        # header 送出時 SSE 可能還在驗證 session（查資料庫）。這時斷線的話，SQLAlchemy 會在
        # 背景關閉被中斷的連線，測試結束、關閉 event loop 時就會卡在等它。
        # 所以等到已經訂閱事件（驗證完、開始等待）才斷線
        hub = env.app.state.event_hub
        for _ in range(200):
            if hub._subscriptions:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("SSE 沒有開始等待事件")
    finally:
        disconnected.set()
        await asyncio.wait_for(task, 2)
    headers = dict(start["headers"])
    assert start["status"] == 200
    assert headers[b"content-type"].startswith(b"text/event-stream")
    assert b"content-encoding" not in headers
