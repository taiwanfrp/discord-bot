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
- [x] 用 docker compose 準備 MySQL、MariaDB、PostgreSQL、Redis、Silo（`pgsty/silo`，MinIO 的社群分支，本機測試 S3），三種資料庫都跑過 `alembic upgrade head` 和測試
  - MySQL 8.4、MariaDB 11.8、PostgreSQL 18 放在 `compose.yaml` 的 `db` profile；`uv run pytest --db mysql` 等，migration 測試也包括 upgrade、check、downgrade 再 upgrade
  - 找到並修正：MySQL / MariaDB 的 downgrade 不能先刪外鍵用到的索引；MariaDB 11.8 在 `REPEATABLE READ` 下帶條件的 UPDATE 會失敗（錯誤 1020），連線改用 `READ COMMITTED`
  - 資料表自己指定 `utf8mb4`，不依賴資料庫的預設字元集；測試用的 MySQL / MariaDB 預設故意設成 `latin1` 來確認

## 2. Queue 與 worker

- [x] taskiq + Redis Streams broker（支援 ack），queue 名稱與 key 前綴 `ticket-bot:`
- [x] worker 服務：只使用 Discord REST API，不連 Discord gateway
- [x] scheduler 服務
- [x] 重試（指數退避）與次數上限
- [x] 優雅關閉：收到 `SIGTERM` 時停止接新工作，等目前的工作完成
- [x] worker → bot 的事件通知（Redis pub/sub）

## 3. API 基礎

- [x] 在 bot 的 event loop 裡啟動 FastAPI（uvicorn）；收到 `SIGTERM` 時正常關閉
- [x] uvicorn 的 `--proxy-headers`、`--forwarded-allow-ips`（只信任自己的 proxy）
- [x] CORS 白名單、檢查 `Origin`、只接受 JSON
- [x] `POST /auth/exchange`：用 `GETDEL` 兌換授權碼，產生 ticket session 並回傳 `__Host-` cookie；`POST /auth/logout`（見 `docs/auth.md`）
- [x] 每個請求的驗證：ticket session → 主要 API 的 session → Discord ID，查詢失敗回傳 503
- [x] 權限判斷（管理員、分類身份組），從伺服器成員快取讀取身份組；不在伺服器裡的使用者改用 REST API 取得資料，只能存取自己的客服單（開單者的判斷在客服單的 API 裡做）
- [x] `GET /health`、`GET /me`
- [x] gzip 壓縮回應（SSE 不壓縮）
- [x] `GET /guild/channels`、`GET /guild/roles`
- [ ] 主要 API 依照 `docs/auth.md` 第 5 節實作：`__Host-session` cookie、session 的格式、`POST /auth/ticket-code`（不在這個 repo）
- [ ] session store 的專用 ACL 帳號（只能讀取 session、讀取並刪除授權碼）

## 4. 設定 API

- [x] 設定：`GET`、`PATCH /settings`
- [x] 分類 CRUD
- [x] 身份組：在 Discord 建立、修改、刪除，以及設定用途（`permission_role`）
- [x] `POST /panel/publish`：發送或更新面板（persistent view，沒有分類時停用下拉選單）
- [x] 在 Discord 直接刪除身份組或頻道時，清掉資料庫裡指向它們的設定
- [x] 實際在 Discord 上測試：發布面板、建立與刪除身份組

## 5. 開單

- [x] 共用的開單流程：在開單者的鎖裡檢查上限（3 張）並建立資料，再建立頻道、權限覆寫、webhook、embed 與三個按鈕
- [x] Discord 的 Modal 送出後先 `defer`，完成後用 followup 回覆
- [x] 頻道名稱格式（只替換白名單變數）
- [x] 失敗時的復原（刪除頻道、標記為 failed）
- [x] Discord：在面板的下拉選單選擇分類後跳出 Modal（`bot/tickets/panel.py` 的 `CategorySelect.callback`），並把下拉選單恢復成未選擇
- [x] 網頁：`POST /tickets`
- [x] 開單者不在伺服器：照常開單，`on_member_join` 時加進頻道，scheduler 每 5 分鐘放入 `sync_pending_openers` 補檢查
- [x] 實際測試：`POST /tickets` 開單、worker 透過 REST API 把開單者加進頻道
- [x] 實際測試：在 Discord 上從面板開單（Modal、`defer`、下拉選單恢復成未選擇）

## 6. 客服單操作

- [x] 三個按鈕的功能（`bot/tickets/controls.py`，persistent view 已經在第 5 步註冊）
- [x] 接手（分類身份組、管理員）；釋放、轉交（目前接手的人、管理員）
- [x] 關閉（確認）、附原因關閉（Modal）
- [x] `/ticket add`、`/ticket remove`（對象不能是開單者或客服）、`/ticket transfer`、`/ticket close`
- [x] 對應的 API：claim、unclaim、transfer、close、members
- [x] log 頻道通知（開單、關單）
- [x] 修改權限身份組（`PUT /permission-roles`）時，同步更新開啟中客服單頻道的權限覆寫（worker 的 `sync_ticket_permissions`）
- [x] 每個操作都寫入 `ticket_event`
- [x] 客服單頻道在 Discord 上被直接刪除時，把客服單標記為關閉（`on_guild_channel_delete`）
- [x] 實際測試：API 的接手、釋放、附原因關單，worker 同步權限覆寫
- [x] 實際測試：在 Discord 上按按鈕（關閉的確認、附原因關閉的 Modal、接手與釋放）與 `/ticket` 指令

## 7. Discord → 網頁同步

- [x] `on_message` 加密後保存訊息，更新 `last_message_at`（略過自己的 webhook）；開啟 Message Content intent
- [x] 使用者快照：每次有動作時更新 `ticket_user`（關單後不再更新）；scheduler 每分鐘放入 `mirror_avatars` 把頭像存到 S3，用 hash 去重；還沒存好之前先用 Discord 的頭像網址
- [x] `mirror_attachment`：串流下載後用 multipart upload 上傳到 S3，並檢查檔案類型；`requeue_pending_attachments` 補放沒放進 queue 的附件
- [x] SSE：`GET /tickets/{id}/events`、`GET /events`；所有事件先寫入 `ticket_event`（commit 後自動推送），支援 `Last-Event-ID` 補送和 heartbeat
- [x] 實際測試：在客服單頻道傳訊息與附件（圖片、偽裝的執行檔、超過 8 MiB 的檔案），頭像存到 Silo，SSE 收到事件、補送與重新驗證

## 8. 網頁 → Discord 同步

- [x] `POST /tickets/{id}/attachments`（上傳網址、multipart 的 `parts`）、`complete`、`DELETE`，上傳完成後的大小與類型檢查（`verify_attachment`）；沒有送出的附件 24 小時後刪除
- [x] bucket 的 CORS（`create_bucket` 設定）
- [x] `POST /tickets/{id}/messages`：加密寫入，交給 worker 用 webhook 送出（附件為分享連結、不通知任何人）
- [x] 分享連結 `GET /a/{attachment_id}/{token}`：比對 token 的雜湊、302 轉址、撤銷、速率限制
- [x] 實測 Discord 抓取預覽時是否跟著 302 轉址（用短網址服務轉址到 AWS S3 測試，會跟著轉址）
- [x] `deliver_messages(ticket_id)`：取得客服單的鎖，依照順序送出所有待送的訊息；webhook 被刪除時重新建立；`requeue_pending_messages` 補放
- [x] 實際測試（Silo）：單一上傳、分塊上傳、大小不符被 S3 拒絕、偽裝的執行檔、訊息等附件檢查完才送出、webhook 名稱的限制、分享連結轉址
- [ ] 實際測試：用公開的 S3 與 bot 網址，從網頁上傳到 Discord 顯示預覽（docs/tickets.md 第 17 節）

## 9. 關單、紀錄與評分

- [x] 關單時撤銷這張單所有的分享連結（第 6 步的關單流程已經做了）
- [x] `send_close_notifications`：關單後私訊開單者（連結 + 評分）、接手的人（連結），以及 `requeue_close_notifications`
- [x] `GET /tickets`（篩選、分頁）、`GET /tickets/{id}`、`GET /tickets/{id}/messages`
- [x] `GET /attachments/{id}`（每次回傳新的 presigned URL，預設 10 分鐘）
- [x] 評分：私訊按鈕（`DynamicItem`，`custom_id` 帶客服單 id）→ Modal（分數 1–5、評語），以及 `PUT /tickets/{id}/rating`，只能一次
- [x] 實際測試：關單後收到私訊、按鈕與 Modal 評分（私訊顯示分數與評語）、先在網頁評分再按私訊的按鈕、bot 重啟後舊的按鈕還能用、自己接手時只送一則私訊
- [ ] 實際測試：分數填錯時的提示、關閉私訊時記錄失敗、接手的人是另一個帳號時收到只有連結的私訊

## 10. 閒置處理

- [x] `check_inactive_tickets`：scheduler 每 5 分鐘放入
- [x] 4 天沒有訊息：提醒 embed 加「立即關閉」按鈕
- [x] 提醒後 1 天仍然沒有訊息：自動關閉
- [x] 實際測試：提醒（提及開單者、「立即關閉」按鈕）、有新訊息後不會自動關閉、自動關閉後刪除頻道與私訊
- [ ] 實際測試：自動關閉後的 log（要先設定 log 頻道）

## 11. 部署

- [x] Dockerfile（bot、worker、scheduler 共用一個 image）：tini 當 PID 1、非 root 使用者、`.dockerignore` 白名單
- [x] bot 服務設定成先停止舊的再啟動新的；scheduler 只執行一個（寫在 `docs/tickets.md` 11.3，部署平台照著設定）
- [x] 停止的等待時間大於最長的工作（worker 30 分鐘、bot 30 秒）
- [x] 實際測試（本機 Docker）：migration（新的資料庫、已經是最新版本的資料庫）、三個服務啟動、`SIGTERM` 後正常停止、worker 等跑到一半的工作完成、強制停止時工作留在 pending、沒有 tini 時 scheduler 會忽略 `SIGTERM`
- [x] bot、scheduler 用 Redis 鎖保證同一時間只有一個（`ticket-bot:lock:bot`、`ticket-bot:lock:scheduler`）：取不到就等、定時延長、被拿走時用非 0 結束；scheduler 收到 `SIGTERM` 時正常關閉並釋放鎖
- [x] 實際測試（本機 Docker）：第二個 bot、scheduler 會等待，超過鎖的有效期限也不會拿到；第一個停止後 1 秒內接手；等待中的被停止時馬上結束；鎖被拿走時用 exit 1 結束
- [x] GitHub Actions CI：pre-commit 的檢查、四種資料庫的 pytest、建立 image 並跑 smoke test（migration、import 三個服務的模組）
- [ ] 在 GitHub 上確認 CI 第一次執行通過
- [ ] 在部署平台上設定並部署，順便做第 8 步用公開的 S3 與 bot 網址的測試

## 12. 之後再做

- [ ] 訊息編輯、刪除的同步
- [ ] Modal 的分數改成下拉選單（確認 discord.py 支援後）
- [ ] 匯出成 HTML 檔
