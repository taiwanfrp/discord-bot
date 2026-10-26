# bot、worker、scheduler 共用這個 image，只有啟動指令不同（docs/tickets.md 11.3）
FROM python:3.12.15-slim-trixie AS build

COPY --from=ghcr.io/astral-sh/uv:0.11.18 /uv /bin/uv
# 套件先編譯成 .pyc，啟動比較快；使用 image 裡的 Python，不另外下載
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0

WORKDIR /app
# 先只安裝依賴：只改程式碼時，這一層可以直接用快取
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project

# 放進來的檔案由 .dockerignore 的白名單決定
COPY . .
# 執行時的使用者不能寫入 /app，先編譯好
RUN python -m compileall -q bot migrations


FROM python:3.12.15-slim-trixie

# tini 當 PID 1，把 SIGTERM 轉給程式：PID 1 沒有自己處理的訊號會被忽略，
# bot 與 scheduler 等鎖的時候還沒有處理 SIGTERM，沒有 tini 的話要等到被強制停止
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --system --uid 10001 --no-create-home app

WORKDIR /app
COPY --from=build /app /app
# 使用 SQLite 時（DATABASE_URL 的預設路徑 data/bot.db），把 volume 掛在 /app/data
RUN mkdir data && chown app:app data

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    # 容器裡要聽所有介面；port 只讓自己的 reverse proxy 連進來（docs/auth.md 第 8 節）
    API_HOST=0.0.0.0

USER app
EXPOSE 8000
ENTRYPOINT ["tini", "--"]
# bot；worker、scheduler、migration 的指令見 docs/tickets.md 11.3
CMD ["python", "main.py"]
