# TaiwanFRP Discord Bot

TaiwanFRP 的 Discord bot，只服務單一伺服器。主要功能是客服單（ticket）系統，並提供 HTTP API 給前端（網頁）串接。背景工作由獨立的 worker 透過 Redis queue 處理。

- 設計文件：`docs/tickets.md`（架構、權限、資料表、API 都以這份為準）
- 身分驗證：`docs/auth.md`（前端在背景用授權碼換取 bot 自己的 session cookie；bot 每次都確認主要 API 的 session 還存在）
- 進度追蹤：`TODO.md`

## 開發流程

- 程式碼必須通過 ruff、mypy（strict）、pytest。
- GitHub Actions（`.github/workflows/ci.yml`）在 push 到 main 與 PR 時執行：pre-commit 的檢查、四種資料庫的 pytest、建立 Docker image 並跑 smoke test。uv 的版本跟 `Dockerfile` 一樣，升級時兩邊一起改。
- 每個測試（包括 fixture 的收尾）最多 60 秒（pytest-timeout，設定在 `pyproject.toml`），卡住時會直接失敗並印出卡在哪裡。
- 一個步驟完成就 commit 一次。commit 訊息用 Conventional Commits，**一句話**，例如 `feat: add ticket panel`。
- 文件裡的圖使用 Mermaid。

## 常用指令

```bash
uv sync                              # 安裝依賴
docker compose up -d                 # 本機開發用的 Redis、Silo（S3）
uv run python -m bot.storage.create_bucket   # 建立本機開發用的 bucket、設定 CORS（第一次啟動 Silo 後）
uv run main.py                       # 啟動 bot
uv run taskiq worker bot.worker.broker:broker bot.worker.tasks --workers 1   # 啟動 worker
uv run taskiq scheduler bot.worker.scheduler:scheduler bot.worker.tasks     # 啟動 scheduler（重試也靠它）
uv run ruff check --fix              # lint（含 import 排序）
uv run ruff format                   # 格式化
uv run mypy                          # 型別檢查（strict）
uv run pytest                        # 測試（每個測試一個新的 SQLite 檔案）
docker compose --profile db up -d --wait   # 測試用的 MySQL、MariaDB、PostgreSQL（資料不保留）
uv run pytest --db mysql             # 換成其他資料庫測試：mysql、mariadb、postgres
uv run pre-commit run --all-files    # 跑全部 hook（只會檢查 git 已追蹤的檔案）
uv run alembic upgrade head          # 套用 migration
uv run alembic revision --autogenerate -m "add ..."   # 修改 models 後產生 migration（產生後要檢查內容）
docker build -t taiwanfrp-discord-bot .   # 建立 image（三個服務共用，部署方式見 docs/tickets.md 11.3）
```

pre-commit：commit 時跑 ruff、mypy 與基本檢查，push 前跑 pytest。

## 專案結構

```
main.py              # 入口：讀取設定並啟動 bot
bot/
├── core/            # 共用程式碼：Bot（自動載入 cogs、全域同步 slash command、啟動 API）、設定、Redis 與事件通知、只能有一個在執行的服務用的鎖（bot、scheduler）、權限、Cog 基底類別、CommandTree（錯誤處理）
├── api/             # HTTP API（FastAPI，跟 bot 在同一個程序）：routes、dependencies、CSRF
├── db/              # 資料庫：models、自訂型別（UTCDateTime、EncryptedText）、engine 與 session、commit 後推送 ticket_event 的 hook
├── storage/         # S3（aiobotocore）、附件的檔案規則（副檔名、magic bytes、key）、附件與頭像存到 S3、網頁上傳（presigned URL、檢查）、分享連結
├── tickets/         # 客服單的共用邏輯與 Discord 介面：開單流程、使用者快照、面板、按鈕、頻道名稱格式、Discord 訊息的保存與編輯、刪除、網頁訊息與附件、用 webhook 送到 Discord、關單私訊與評分、閒置提醒與自動關閉
├── worker/          # 背景工作：broker（queue、重試）、scheduler、tasks
└── cogs/            # 每個模組（或含 __init__.py 的資料夾）都會被自動載入
migrations/          # Alembic migration
tests/               # tests/api/：API 測試（helpers.py 有假的 bot、伺服器與登入）；測試一律用 fakeredis，不可以連到真的 Redis 或 S3
docs/tickets.md      # 客服單系統設計
docs/auth.md         # 身分驗證
TODO.md
Dockerfile           # bot、worker、scheduler 共用的 image；放進去的檔案由 .dockerignore 的白名單決定
.github/workflows/ci.yml   # CI：pre-commit、四種資料庫的測試、image 的 smoke test
```

## 程式慣例

- Python 3.12，所有函式都要有型別標註，必須通過 `mypy --strict`。
- bot 使用 `members`、`message_content` 兩個特權 intent（Developer Portal 要開啟）。
- 只使用 slash command，不使用前綴指令。指令是**全域同步**，伺服器與私訊都可用；只能在伺服器使用的指令加 `@app_commands.guild_only()`。
- `bot/cogs/` 底下的每個模組都必須有 `async def setup(bot)`，因為 `pkgutil.iter_modules` 會全部載入。輔助程式放在 `bot/core/` 或其他套件，不要放進 `cogs/`。
- cog 繼承 `bot.core.cog.Cog`（已提供 `self.bot`）。
- bot 代替使用者送出的內容（例如網頁訊息的 webhook）一律 `allowed_mentions=discord.AllowedMentions.none()`，裡面的 `@everyone` 等只顯示、不通知。
- 要讓使用者看到的錯誤，丟出 `bot.core.tree.UserFacingError("訊息")`；其餘未預期的錯誤由 `CommandTree.on_error` 統一記錄並回覆。
- 使用 `logging.getLogger(__name__)`，不要用 `print`。
- 設定一律來自環境變數或 `.env`，透過 `bot.core.config.get_settings()` 讀取，不要寫死在程式裡；`.env` 不可 commit，新增設定時同步更新 `Settings` 與 `.env.example`。
- 新增執行時需要的檔案或資料夾（不在 `bot/`、`migrations/` 裡的）時，要加進 `.dockerignore` 的白名單，否則不會被放進 image。

## 資料庫規則（SQLite / MySQL、MariaDB / PostgreSQL 都要支援）

- 使用 SQLAlchemy 2.0 async 與 Alembic；資料表結構的任何變更都必須寫 migration。
- migration 要跟上一版的程式相容，因為部署時新舊版本會同時執行一段時間：新增欄位要允許 NULL 或有預設值；刪除欄位或改名分兩次部署（docs/tickets.md 11.3）。
- 開發時使用 SQLite（`data/bot.db`）。
- Discord ID（snowflake）一律用 `BigInteger`，model 裡寫 `Mapped[Snowflake]`（`bot.db.base`）。
- `String` 一定要指定長度；長文字用 `Text`。
- Enum 使用 `native_enum=False`（存成字串）。model 裡的 enum 繼承 `StrEnum`，會自動存成它的值。
- 時間一律以 UTC 儲存。model 裡寫 `Mapped[datetime]` 會自動使用 `UTCDateTime`（只接受帶時區的 datetime），現在時間用 `bot.db.types.utcnow()`；不使用資料庫的 `now()` 預設值，因為各資料庫的時區行為不同。
- 自增主鍵與外鍵用 `Integer`（SQLite 只有 `INTEGER PRIMARY KEY` 會自動遞增）。
- MySQL / MariaDB 使用 `utf8mb4`：連線網址加 `?charset=utf8mb4`；資料表在 `bot/db/base.py` 自己指定，不依賴資料庫的預設。新增資料表的 migration 要有 `mysql_charset="utf8mb4"`（自動產生的會帶上，`--db mysql` 的測試會檢查）。連線的隔離等級是 `READ COMMITTED`（`bot/db/engine.py`，原因見 docs/tickets.md 13.1）。
- 修改 models、migration，或是用到交易、鎖、帶條件的 UPDATE 的程式時，用 `--db mysql`、`--db mariadb`、`--db postgres` 都跑過測試。
- migration 的 downgrade 刪除資料表時直接 `drop_table`，不要先刪它的索引（MySQL / MariaDB 不能刪除外鍵用到的索引；Alembic 自動產生的會先刪，要改掉）。
- 測試的資料庫一律用 `tests/database.py` 的 `create_test_engine(tmp_path)`，不要寫死 SQLite，才能用 `--db` 換成其他資料庫。
- MetaData 設定 `naming_convention`；Alembic 開啟 `render_as_batch=True`（SQLite 需要）。
- 不使用任何單一資料庫專屬的語法或型別。
- 標示加密的欄位（訊息內容等）使用 `EncryptedText` 型別；加密金鑰只來自環境變數。
- 客服單裡任何存使用者 Discord ID 的動作，都要同時更新 `ticket_user` 的快照（名稱與頭像）；客服單關閉後不再更新。

## API 規則

- Discord ID 在 JSON 裡一律用字串（snowflake 超過 JavaScript 的安全整數範圍）。
- 會修改資料的請求要檢查 `Origin`，並且只接受 `application/json`。
- 權限一律在 bot 端判斷，不能相信前端送來的身分或角色資料。
- route 放在 `bot/api/routes/`，用 `bot/api/deps.py` 的 dependency 取得資料：`ViewerDep`（需要登入）、`AdminDep`（只有管理員）、`GuildDep`、`DBDep`、`RedisDep`、`S3Dep`（bot 啟動時建立的 S3 client）。不要在 route 裡直接呼叫 `get_redis()`、`s3_client()` 等函式，測試才能替換。
- 客服單的流程寫在 `bot/tickets/`，Discord 與 API 共用；要讓使用者看到的錯誤丟出 `bot.tickets.open.TicketError(訊息, status_code)`，API 轉成 HTTP 錯誤，Discord 回覆訊息。
- 權限用 `viewer.access`（`bot.core.permissions.Access`）：`is_admin`、`is_staff(category_id)`。開單者的判斷是 `viewer.id == ticket.opener_id`。
- 被刪除的訊息（`deleted_at`）、編輯前的內容（`ticket_message_revision`）、編輯時移除的附件（`removed_at`），網頁上只有這張單的客服、管理員看得到（`is_staff(ticket.category_id)`）。新增讀取訊息或附件的地方（例如匯出）時要一樣處理。
- request / response 的格式寫在 `bot/api/schemas.py`；Discord ID 的欄位用那裡的 `Snowflake` 型別（輸出時轉成字串）。

## 背景工作規則

- 資料庫是唯一的真實狀態：工作只傳 id，執行時再從資料庫讀取最新資料。
- 每個工作都必須冪等（重複執行結果相同），因為部署時被中斷的工作會重新執行。
- 修改工作的參數時要跟上一版相容：部署期間，舊版本的 worker 可能收到新版本放入的工作，反過來也一樣。
- 附件一律使用串流處理，不可以把整個檔案讀進記憶體（上限 1 GB）。
- 附件儲存只依賴 S3 API，不可以使用特定服務商（例如 R2）專屬的功能。
- Redis 的 key 一律加上 `ticket-bot:` 前綴（與主要 API 約定的 `session:`、`ticket_code:` 除外）；不要用 db 編號區分資料，pub/sub 不分 db。
- queue 與鎖使用單一主節點的 Redis（`REDIS_URL`），不可以放在多主架構的 session store 上。
- 工作寫在 `bot/worker/tasks.py`，用 `@broker.task` 定義；需要 Discord 時加上參數 `discord_client: discord.Client = DiscordDep`（`bot.worker.broker`，只有 REST API，沒有快取）。要用預設值的寫法，`.kiq()` 才不需要傳這個參數；不要命名為 `discord`，會蓋掉 `discord` 模組。
- worker 的 Discord client 沒有快取（`guild.get_channel` 一定是 None）：要用頻道時用 `bot.tickets.channels.get_text_channel`，快取裡沒有時才用 REST 取得。
- 失敗的工作會自動重試，間隔 1、2、4、8、16 分鐘，由 scheduler 放回 queue。不需要重試的錯誤要自己處理掉，不要丟出例外。
- 最後一次重試也失敗時要做的處理（例如標記為 `failed`）：加上參數 `context: Context = ContextDep`，用 `is_last_attempt(context)` 判斷（`bot.worker.broker`）。
- 工作執行超過 30 分鐘還沒結束，會被其他 worker 接手重新執行（`idle_timeout`）。可能超過的工作要先取得鎖，避免同時執行兩次。取不到鎖時要丟出例外讓它稍後重試，不要直接結束：持有鎖的 worker 可能已經當機，直接結束的話工作會被 ack，就再也不會執行（scheduler 定時執行的掃描工作可以直接結束，下一次會補上）。
- `ticket_event` 一律用 ORM 新增（`db.add(TicketEvent(...))`），commit 之後會自動透過 Redis 通知 bot 並推送 SSE（`bot/db/hooks.py`），bot 與 worker 都一樣，不要自己呼叫 `publish_event`。用 `insert()` 等 Core 語法新增的不會推送。bot 的 cog 用 `on_ticket_event(event_id)` 接收。
- 例外的 key：taskiq-redis 會使用 `autoclaim:ticket-bot:worker:ticket-bot:queue` 當作鎖，設定 Redis ACL 時要一併允許。
