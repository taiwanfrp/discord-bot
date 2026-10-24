# TaiwanFRP Discord Bot

TaiwanFRP 的 Discord bot，只服務單一伺服器。主要功能是客服單（ticket）系統，並提供 HTTP API 給前端（網頁）串接。背景工作由獨立的 worker 透過 Redis queue 處理。

- 設計文件：`docs/tickets.md`（架構、權限、資料表、API 都以這份為準）
- 身分驗證：`docs/auth.md`（前端在背景用授權碼換取 bot 自己的 session cookie；bot 每次都確認主要 API 的 session 還存在）
- 進度追蹤：`TODO.md`

## 開發流程

- 程式碼必須通過 ruff、mypy（strict）、pytest。
- 一個步驟完成就 commit 一次。commit 訊息用 Conventional Commits，**一句話**，例如 `feat: add ticket panel`。
- 文件裡的圖使用 Mermaid。

## 常用指令

```bash
uv sync                              # 安裝依賴
uv run main.py                       # 啟動 bot
uv run ruff check --fix              # lint（含 import 排序）
uv run ruff format                   # 格式化
uv run mypy                          # 型別檢查（strict）
uv run pytest                        # 測試
uv run pre-commit run --all-files    # 跑全部 hook（只會檢查 git 已追蹤的檔案）
```

pre-commit：commit 時跑 ruff、mypy 與基本檢查，push 前跑 pytest。

## 專案結構

```
main.py              # 入口：讀取設定並啟動 bot
bot/
├── core/            # 共用程式碼：Bot（自動載入 cogs、全域同步 slash command）、設定、Cog 基底類別、CommandTree（錯誤處理）
└── cogs/            # 每個模組（或含 __init__.py 的資料夾）都會被自動載入
tests/
docs/tickets.md      # 客服單系統設計
docs/auth.md         # 身分驗證
TODO.md
```

## 程式慣例

- Python 3.12，所有函式都要有型別標註，必須通過 `mypy --strict`。
- 只使用 slash command，不使用前綴指令。指令是**全域同步**，伺服器與私訊都可用；只能在伺服器使用的指令加 `@app_commands.guild_only()`。
- `bot/cogs/` 底下的每個模組都必須有 `async def setup(bot)`，因為 `pkgutil.iter_modules` 會全部載入。輔助程式放在 `bot/core/` 或其他套件，不要放進 `cogs/`。
- cog 繼承 `bot.core.cog.Cog`（已提供 `self.bot`）。
- 要讓使用者看到的錯誤，丟出 `bot.core.tree.UserFacingError("訊息")`；其餘未預期的錯誤由 `CommandTree.on_error` 統一記錄並回覆。
- 使用 `logging.getLogger(__name__)`，不要用 `print`。
- 設定一律來自環境變數或 `.env`，透過 `bot.core.config.get_settings()` 讀取，不要寫死在程式裡；`.env` 不可 commit，新增設定時同步更新 `Settings` 與 `.env.example`。

## 資料庫規則（SQLite / MySQL、MariaDB / PostgreSQL 都要支援）

- 使用 SQLAlchemy 2.0 async 與 Alembic；資料表結構的任何變更都必須寫 migration。
- Discord ID（snowflake）一律用 `BigInteger`。
- `String` 一定要指定長度；長文字用 `Text`。
- Enum 使用 `native_enum=False`（存成字串）。
- 時間一律以 UTC 儲存。
- MySQL / MariaDB 使用 `utf8mb4`。
- MetaData 設定 `naming_convention`；Alembic 開啟 `render_as_batch=True`（SQLite 需要）。
- 不使用任何單一資料庫專屬的語法或型別。
- 標示加密的欄位（訊息內容等）使用 `EncryptedText` 型別；加密金鑰只來自環境變數。
- 客服單裡任何存使用者 Discord ID 的動作，都要同時更新 `ticket_user` 的快照（名稱與頭像）；客服單關閉後不再更新。

## API 規則

- Discord ID 在 JSON 裡一律用字串（snowflake 超過 JavaScript 的安全整數範圍）。
- 會修改資料的請求要檢查 `Origin`，並且只接受 `application/json`。
- 權限一律在 bot 端判斷，不能相信前端送來的身分或角色資料。

## 背景工作規則

- 資料庫是唯一的真實狀態：工作只傳 id，執行時再從資料庫讀取最新資料。
- 每個工作都必須冪等（重複執行結果相同），因為部署時被中斷的工作會重新執行。
- 附件一律使用串流處理，不可以把整個檔案讀進記憶體（上限 1 GB）。
- 附件儲存只依賴 S3 API，不可以使用特定服務商（例如 R2）專屬的功能。
- Redis 的 key 一律加上 `ticket-bot:` 前綴（與主要 API 約定的 `session:`、`ticket_code:` 除外）；不要用 db 編號區分資料，pub/sub 不分 db。
- queue 與鎖使用單一主節點的 Redis（`REDIS_URL`），不可以放在多主架構的 session store 上。
