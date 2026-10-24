# TODO

設計細節見 `docs/tickets.md` 與 `docs/auth.md`。完成一項就打勾，一個步驟完成就 commit 一次。

## 已完成

- [x] 專案初始化：uv、discord.py、python-dotenv
- [x] 開發工具：ruff、mypy（strict）、pytest、pre-commit
- [x] `commands.Bot`，全域 slash command（伺服器、私訊都可用）
- [x] 拆成 cogs，自動載入 `bot/cogs/`
- [x] `Cog` 基底類別
- [x] slash command 的全域錯誤處理
- [x] 用 logging 取代 print

## 0. 架構整理

- [x] 把 `Bot` 從 `main.py` 搬到 `bot/core/bot.py`
- [x] 設定改用 pydantic-settings（`bot/core/config.py`）；其他環境變數等到用到的步驟再加進 `Settings` 與 `.env.example`（見 `docs/tickets.md` 第 16 節與 `docs/auth.md` 第 11 節）

## 1. 資料庫

- [x] 安裝 SQLAlchemy（async）、Alembic、aiosqlite、cryptography
- [x] 建立 async engine 和 session，連線來自 `DATABASE_URL`
- [x] Base、`naming_convention`、UTC 時間型別
- [x] `EncryptedText` 型別（MultiFernet，金鑰來自 `ENCRYPTION_KEYS`）
- [x] models：settings、ticket_category、permission_role、ticket、ticket_user、ticket_member、ticket_message、ticket_attachment、ticket_event
- [x] Alembic（async 樣板、`render_as_batch=True`）與第一個 migration
- [x] 主要的索引（見 `docs/tickets.md` 第 13 節）
- [x] 安裝 aiomysql（MySQL、MariaDB）、asyncpg（PostgreSQL）
- [ ] 用 docker compose 準備 MySQL、MariaDB、PostgreSQL、Redis、Silo（`pgsty/silo`，MinIO 的社群分支，本機測試 S3），三種資料庫都跑過 `alembic upgrade head` 和測試

## 2. Queue 與 worker

- [x] taskiq + Redis Streams broker（支援 ack），queue 名稱與 key 前綴 `ticket-bot:`
- [x] worker 服務：只使用 Discord REST API，不連 Discord gateway
- [x] scheduler 服務
- [x] 重試（指數退避）與次數上限
- [x] 優雅關閉：收到 `SIGTERM` 時停止接新工作，等目前的工作完成
- [x] worker → bot 的事件通知（Redis pub/sub）

## 3. API 基礎

- [ ] 在 bot 的 event loop 裡啟動 FastAPI（uvicorn）
- [ ] uvicorn 的 `--proxy-headers`、`--forwarded-allow-ips`（只信任自己的 proxy）
- [ ] CORS 白名單、檢查 `Origin`、只接受 JSON
- [ ] `POST /auth/exchange`：用 `GETDEL` 兌換授權碼，產生 ticket session 並回傳 `__Host-` cookie；`POST /auth/logout`（見 `docs/auth.md`）
- [ ] 每個請求的驗證：ticket session → 主要 API 的 session → Discord ID，查詢失敗回傳 503
- [ ] 權限判斷（管理員、分類身份組、開單者），從伺服器成員快取讀取身份組；不在伺服器裡的使用者改用 REST API 取得資料，只能存取自己的客服單
- [ ] `GET /health`、`GET /me`
- [ ] `GET /guild/channels`、`GET /guild/roles`
- [ ] 主要 API 依照 `docs/auth.md` 第 5 節實作：`__Host-session` cookie、session 的格式、`POST /auth/ticket-code`（不在這個 repo）
- [ ] session store 的專用 ACL 帳號（只能讀取 session、讀取並刪除授權碼）

## 4. 設定 API

- [ ] 設定：`GET`、`PATCH /settings`
- [ ] 分類 CRUD
- [ ] 身份組：在 Discord 建立、修改、刪除，以及設定用途（`permission_role`）
- [ ] `POST /panel/publish`：發送或更新面板

## 5. 開單

- [ ] 共用的開單流程：在開單者的鎖裡檢查上限（3 張）並建立資料，再建立頻道、權限覆寫、webhook、embed 與三個按鈕
- [ ] Discord 的 Modal 送出後先 `defer`，完成後用 followup 回覆
- [ ] 頻道名稱格式（只替換白名單變數）
- [ ] 失敗時的復原（刪除頻道、標記為 failed）
- [ ] Discord：面板的下拉選單（選項來自資料庫，沒有分類時停用）與 Modal
- [ ] 網頁：`POST /tickets`
- [ ] 開單者不在伺服器：照常開單，`on_member_join` 時加進頻道，scheduler 每 5 分鐘放入 `sync_pending_openers` 補檢查

## 6. 客服單操作

- [ ] persistent view（重啟後按鈕還能用）
- [ ] 接手（分類身份組、管理員）；釋放、轉交（目前接手的人、管理員）
- [ ] 關閉（確認）、附原因關閉（Modal）
- [ ] `/ticket add`、`/ticket remove`（對象不能是開單者或客服）、`/ticket transfer`、`/ticket close`
- [ ] 對應的 API：claim、unclaim、transfer、close、members
- [ ] log 頻道通知
- [ ] 每個操作都寫入 `ticket_event`

## 7. Discord → 網頁同步

- [ ] `on_message` 加密後保存訊息，更新 `last_message_at`（略過自己的 webhook）
- [ ] 使用者快照：每次有動作時更新 `ticket_user`（關單後不再更新）；`mirror_avatar` 把頭像存到 S3，用 hash 去重；還沒存好之前先用 Discord 的頭像網址
- [ ] `mirror_attachment`：串流下載後用 multipart upload 上傳到 S3，並檢查檔案類型
- [ ] SSE：`GET /tickets/{id}/events`、`GET /events`；所有事件先寫入 `ticket_event`，支援 `Last-Event-ID` 補送和 heartbeat

## 8. 網頁 → Discord 同步

- [ ] `POST /tickets/{id}/attachments`（上傳網址、multipart）與上傳完成後的類型檢查（`verify_attachment`）
- [ ] `POST /tickets/{id}/messages`：加密寫入，交給 worker 用 webhook 送出（附件為分享連結）
- [ ] 分享連結 `GET /a/{attachment_id}/{token}`：比對 token 的雜湊、302 轉址、撤銷、速率限制
- [ ] 實測 Discord 抓取預覽時是否跟著 302 轉址
- [ ] `deliver_messages(ticket_id)`：取得客服單的鎖，依照順序送出所有待送的訊息

## 9. 關單、紀錄與評分

- [ ] 關單時撤銷這張單所有的分享連結
- [ ] `send_close_notifications`：關單後私訊開單者（連結 + 評分）、接手的人（連結）
- [ ] `GET /tickets`（篩選、分頁）、`GET /tickets/{id}`、`GET /tickets/{id}/messages`
- [ ] `GET /attachments/{id}`（每次回傳新的 presigned URL，預設 10 分鐘）
- [ ] 評分：私訊按鈕（`DynamicItem`，`custom_id` 帶客服單 id）→ Modal（分數 1–5、評語），以及 `PUT /tickets/{id}/rating`，只能一次

## 10. 閒置處理

- [ ] `check_inactive_tickets`：scheduler 每 5 分鐘放入
- [ ] 4 天沒有訊息：提醒 embed 加「立即關閉」按鈕
- [ ] 提醒後 1 天仍然沒有訊息：自動關閉

## 11. 部署

- [ ] Dockerfile（bot、worker、scheduler 共用一個 image）
- [ ] bot 服務設定成先停止舊的再啟動新的；scheduler 只執行一個
- [ ] 停止的等待時間大於最長的工作

## 12. 之後再做

- [ ] 訊息編輯、刪除的同步
- [ ] Modal 的分數改成下拉選單（確認 discord.py 支援後）
- [ ] 匯出成 HTML 檔
