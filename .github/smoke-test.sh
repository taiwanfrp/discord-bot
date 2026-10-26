#!/bin/sh
# image 的 smoke test（ci.yml、build.yml 共用）：.github/smoke-test.sh <image>
# image 裡有需要的檔案與套件（.dockerignore 是白名單）：
# migration 跑得起來，三個服務的模組都 import 得到。設定是假的，不會連到任何服務
set -eu

env_file=$(mktemp)
trap 'rm -f "$env_file"' EXIT
cat > "$env_file" <<'ENV'
DISCORD_TOKEN=ci
GUILD_ID=1
DATABASE_URL=sqlite+aiosqlite:///data/bot.db
REDIS_URL=redis://127.0.0.1:1
SESSION_REDIS_URL=redis://127.0.0.1:1
ENCRYPTION_KEYS=ci
S3_ACCESS_KEY_ID=ci
S3_SECRET_ACCESS_KEY=ci
S3_BUCKET=ci
PUBLIC_BASE_URL=https://bot.test
WEB_TICKET_URL=https://web.test/tickets/{id}
ENV

docker run --rm --env-file "$env_file" "$1" sh -c \
    'alembic upgrade head && python -c "import main, bot.worker.tasks, bot.worker.scheduler"'
