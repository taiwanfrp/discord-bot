# 客服單系統設計

> 狀態：設計已定案，實作時以這份文件為準。需要實測的項目見最後一節。

## 1. 目標與範圍

- 使用者可以從 **Discord 或網頁**開單：選擇分類、填寫主旨與內容，bot 建立私人頻道。
- 開單者可以關閉、附原因關閉、加入或移除其他成員；客服與管理員可以接手、轉交、關閉。
- Discord 與網頁**雙向即時同步**：頻道裡的訊息在網頁看得到，網頁傳的訊息透過 webhook 出現在 Discord。
- **每則訊息在送出的當下就存進資料庫**（內容加密）。關單後刪除頻道，網頁直接用資料庫裡的資料顯示紀錄頁面。
- 太久沒有訊息時先提醒，仍然沒有回應就自動關閉。
- 關單後私訊開單者與接手的客服，附上紀錄頁面的連結；開單者另外可以評分。
- **所有設定都在網頁完成**：分類、身份組、面板頻道、頻道所在的類別、頻道名稱格式、附件限制。資料庫一開始是空的，不預先寫入任何資料。
- bot 只提供 API，網頁介面由前端負責。
- 不在範圍內（目前）：人數統計、多語系。

## 2. 架構

```mermaid
flowchart LR
    browser["瀏覽器（前端：Nuxt CSR）"]

    subgraph botsvc["bot 服務（一個程序）"]
        api["FastAPI（API、SSE）"]
        gateway["discord.py（Discord gateway）"]
    end

    worker["worker 服務"]
    scheduler["scheduler 服務"]
    redis[("Redis（queue、鎖、pub/sub、ticket session）")]
    db[("資料庫（SQLite / MySQL、MariaDB / PostgreSQL）")]
    s3[("S3 相容儲存（附件、頭像）")]
    sessions[("主要 API 的 session store")]
    discord["Discord"]

    browser -- "fetch、EventSource（帶 cookie）" --> api
    browser -- "presigned URL 上傳、下載" --> s3
    api -- 唯讀查詢身分 --> sessions
    gateway <--> discord
    botsvc -- 讀寫 --> db
    botsvc -- 放入工作 --> redis
    scheduler -- 定時放入工作 --> redis
    redis -- 取出工作 --> worker
    worker -- 讀寫 --> db
    worker -- 串流上傳、檢查 --> s3
    worker -- "REST API（webhook、私訊、刪除頻道）" --> discord
    worker -. "事件（pub/sub）" .-> redis
    redis -. "事件（pub/sub）" .-> botsvc
```

- **資料庫由 bot 擁有**：資料表結構與 migration 都在這個 repo。前端不直接連資料庫，只透過 API 存取。
- **API 與 Discord gateway 在同一個程序**：API 可以直接呼叫 bot 執行 Discord 操作。
- **背景工作交給獨立的 worker**（見第 11 節）。worker 不連 Discord gateway，只用 Discord 的 REST API，例如送 webhook、私訊、刪除頻道。
- 三個服務（bot、worker、scheduler）使用**同一份程式碼、同一個 image**，只有啟動指令不同。
- Redis 同時用在 queue、鎖、worker → bot 的事件通知（pub/sub），以及 ticket session（見 `docs/auth.md`）。
- bot 另外會唯讀查詢主要 API 的 session store，確認使用者的身分（見 `docs/auth.md`）。

## 3. 網域、登入與身分驗證

詳細內容見 `docs/auth.md`，這裡只列出重點：

- 網頁帳號只能用 Discord OAuth2 建立，所以每個帳號都有 Discord ID。登入和 session 由主要 API 負責。
- **背景交換授權碼**：前端帶著主要 API 的 cookie 取得一次性的授權碼，交給 bot 換成 bot 自己的 session cookie。每個網域的 cookie 只屬於自己，瀏覽器直接連到 bot。
- bot 每次都確認連結的主要 API session 還存在（直接查詢同地區的 session store），所以主要 API 撤銷 session 時，bot 這邊立刻失效。
- CORS 只允許前端的 origin；會修改資料的請求要檢查 `Origin`，並且只接受 JSON。

## 4. 角色與權限

### 4.1 身份組

身份組可以在網頁上**建立**（由 bot 在 Discord 建立）並**指定用途**。沒有「通用的客服身份組」。

bot 只能建立、修改、指派**排在 bot 自己的身份組下面**的身份組。所以 bot 的身份組要放在這些身份組的上面；在網頁選擇現有的身份組時，排在 bot 上面的要顯示成無法使用。

| 用途 | 數量 | 說明 |
|---|---|---|
| 管理員身份組 | 可多個 | 能做所有事：修改設定、看到所有客服單、接手、關閉 |
| 分類身份組 | 每個分類可多個 | 負責該分類的客服，例如「客服-帳務」 |

有 Discord「管理員」（Administrator）權限的成員也算管理員。這樣在還沒設定任何管理員身份組之前，伺服器的管理員也能從網頁做第一次設定。

直接在 Discord 刪除身份組或頻道時，bot 會清掉資料庫裡指向它們的設定（權限身份組、log 頻道、預設類別、面板、分類的類別）。

### 4.2 權限表

| 操作 | 開單者 | 額外加入的成員 | 分類身份組 | 管理員 |
|---|---|---|---|---|
| 看到頻道、傳訊息 | ✅ | ✅ | ✅ | ✅ |
| 關閉、附原因關閉 | ✅ | ❌ | ✅ | ✅ |
| 加入、移除成員 | ✅ | ❌ | ✅ | ✅ |
| 接手（還沒有人接手時） | ❌ | ❌ | ✅ | ✅ |
| 釋放、轉交給其他客服 | ❌ | ❌ | ✅（只有目前接手的人） | ✅ |
| 評分 | ✅ | ❌ | ❌ | ❌ |
| 修改設定、分類、身份組 | ❌ | ❌ | ❌ | ✅ |

- **加入、移除成員的對象不能是開單者或客服**（有該分類身份組或管理員身份組的人）。開單者不能被移除；客服本來就能透過身份組看到頻道，不需要另外加入。
- **接手**：已經有人接手的客服單不能直接搶過來，要由目前接手的人或管理員轉交。
- **轉交**：只能轉給有該分類身份組或管理員身份組的人，**不能換分類**。
- 網頁上看得到的客服單範圍，跟 Discord 上的權限相同。bot 從伺服器成員快取讀取使用者的身份組來判斷。
- 每個人**同時最多開 3 張**客服單（可以在網頁修改）。

## 5. 開單流程

### 5.1 從 Discord 開單

```mermaid
flowchart TD
    panel["面板訊息（下拉選單：所有啟用中的分類）"]
    modal["Modal：主旨 Subject（短文字，必填）、內容 Content（長文字，必填）"]
    flow["共用的開單流程（5.3）"]

    panel -- 選擇分類 --> modal
    modal -- 送出 --> flow
```

- 面板的選項來自資料庫，只顯示啟用中的分類（依照 `position` 排序，最多 25 個，這是 Discord 下拉選單的上限）。網頁修改分類之後，呼叫 `POST /panel/publish` 讓 bot **編輯**現有的面板訊息，選項才會跟著更新。
- `POST /panel/publish` 帶要發布的頻道（`{"channel_id": "..."}`）。同一個頻道已經有面板時編輯它；換了頻道時送出新的面板，並刪除舊頻道裡的面板。
- 沒有任何啟用中的分類時，面板顯示「目前沒有可用的分類」，並停用下拉選單。

### 5.2 從網頁開單

`POST /tickets`，內容包含分類、主旨、內容（也可以附加檔案），之後走同一個開單流程。Discord 頻道會同時建立，開單者在 Discord 上也看得到這張單。

#### 開單者不在 Discord 伺服器裡

網頁帳號一定有 Discord ID，但**不一定在 Discord 伺服器裡**。最常見的情況是：帳號被盜用而濫發訊息，被 bot 停權，使用者只能從網頁開單申請解除停權。

1. **照常開單**：建立資料與頻道，但開單者先不加進頻道的權限覆寫（`opener_added_at` 為 null）。對話只能在網頁上進行，客服在 Discord 頻道裡回覆，網頁一樣看得到。
2. 開單的 embed 標示「開單者目前不在伺服器內」；如果開單者被停權，也一併標示出來，方便客服判斷。
3. **開單者加入伺服器時**（例如解除停權後重新加入）：
   - `on_member_join` 收到事件，立刻把開單者加進他所有開啟中客服單的頻道，並在頻道裡送出通知。
   - scheduler 每 5 分鐘也放入一次檢查工作，處理 bot 離線時錯過的加入事件。
4. 客服單關閉後就停止等待。
5. 開單者不在伺服器的期間，bot 無法私訊他，所以關單後的通知與評分只能在網頁上進行。

### 5.3 共用的開單流程

1. 檢查：分類仍然啟用中、開單者沒有超過同時開單的上限。
2. 建立 ticket 資料，取得自增的 id。
3. 依照頻道名稱格式，在目標類別底下建立私人頻道，並設定權限。
4. 在頻道裡建立 webhook，存進資料庫。
5. 送出 embed（分類、主旨、內容、開單者、時間、來源）與三個按鈕。
6. 失敗時：刪除已建立的頻道，客服單標記為 `failed`，回覆使用者錯誤訊息。

- **從 Discord 開單時，要先 defer**：Discord 規定 interaction 必須在 3 秒內回應，而建立頻道、webhook 加起來很可能超過 3 秒。所以 Modal 送出後，先用 `interaction.response.defer(ephemeral=True)` 回應，完成後再用 followup 告訴使用者頻道在哪裡。
- **開單上限要防止同時送出**：同一個人同時送出好幾次開單，可能每一次檢查時都還沒超過上限。所以步驟 1、2 要在**同一個開單者的鎖**（Redis lock，例如 `ticket-bot:lock:open:{discord_id}`）裡面執行。

## 6. 客服單頻道

### 6.1 所在的 Discord 類別

```
實際使用的類別 = ticket_category.discord_category_id（分類有設定時）
             或 settings.default_discord_category_id（全域預設）
```

網頁可以只設定全域預設（所有分類共用一個類別），也可以在每個分類各自設定。兩者都沒有設定時就無法開單，並提示管理員到網頁設定。

注意：一個 Discord 類別最多只能放 50 個頻道。

### 6.2 頻道名稱格式

- 在網頁設定 `settings.channel_name_template`，預設是 `客服單-{id}`。
- 可以使用的變數：`{id}`、`{category}`（分類名稱）、`{opener}`（開單者名稱）。`{id:4}` 表示補零到 4 位數，例如 `0012`。
- **只替換白名單內的變數，不可以用 `str.format()`**，因為它可以讀取物件屬性，有安全風險。
- Discord 會自動轉成小寫、把空格換成 `-`，名稱最長 100 個字元。超過時截斷。

### 6.3 權限覆寫

| 對象 | 權限 |
|---|---|
| `@everyone` | 禁止查看 |
| bot | 查看、傳送、嵌入連結、讀取訊息紀錄、管理頻道、管理權限、管理 webhook |
| 開單者、額外加入的成員 | 查看、傳送、附加檔案、讀取訊息紀錄 |
| 該分類的分類身份組、管理員身份組 | 同上 |

bot 在伺服器層級需要的權限：管理頻道、管理身份組、管理 webhook、查看頻道、傳送訊息、嵌入連結、讀取訊息紀錄。

## 7. 按鈕與指令

客服單頻道裡的三個按鈕（接手之後，「接手」會換成「釋放」）：

| 按鈕 | 行為 | 誰可以用 |
|---|---|---|
| 關閉 | 跳出確認 → 關單 | 開單者、分類身份組、管理員 |
| 附原因關閉 | 跳出 Modal 填寫原因 → 關單 | 開單者、分類身份組、管理員 |
| 接手 | 記錄接手的人，更新 embed；按鈕改成「釋放」 | 分類身份組、管理員（還沒有人接手時） |
| 釋放（接手後才出現） | 清除接手的人，按鈕改回「接手」 | 目前接手的人、管理員 |

- 使用 persistent view：`timeout=None` 加上固定的 `custom_id`，並在 `setup_hook` 裡註冊，bot 重啟後按鈕還能使用。客服單頻道裡的按鈕（包括閒置提醒的「立即關閉」），按下時用 `channel_id` 查出是哪一張客服單。
- **私訊裡的評分按鈕不在客服單頻道裡**，無法用 `channel_id` 查詢，而且頻道也已經刪除了。所以評分按鈕要把客服單 id 放進 `custom_id`（例如 `ticket:rate:12`），使用 discord.py 的 `DynamicItem` 解析，bot 重啟後一樣能使用。
- slash command：`/ticket add`、`/ticket remove`（加入、移除成員）、`/ticket transfer`（轉交）、`/ticket close [reason]`。

## 8. 訊息同步

**每一則訊息在送出的當下就寫進資料庫**，不是等到關單才保存。所以紀錄頁面隨時都是最新的，就算頻道被誤刪，紀錄也不會遺失。

### 8.1 Discord → 網頁

1. `on_message`：只處理客服單頻道裡的訊息，並略過：
   - `message.webhook_id` 是這張客服單自己的 webhook 的訊息（那是從網頁送出的訊息，已經存過了）。
   - bot 自己送出的訊息（開單的 embed、閒置提醒等）。這些動作記錄在 `ticket_event`，不算是對話內容。
2. 加密後存進 `ticket_message`，更新作者的快照（8.6），並更新 `ticket.last_message_at`。
3. 附件：每個附件先存一筆 `pending` 的 `ticket_attachment`，再放入 worker 工作 `mirror_attachment`（下載後上傳到 S3）。**一定要上傳到 S3**，因為 Discord 的附件網址大約一天就會過期。
4. 送出 SSE 事件 `message.created`。
5. 編輯、刪除：用 `on_message_edit`、`on_message_delete` 更新資料庫，並送出對應的事件（第二階段再做）。

### 8.2 網頁 → Discord

1. 附件：前端呼叫 `POST /tickets/{id}/attachments` 取得上傳網址，**直接上傳到 S3，不經過 bot**。大檔案使用 multipart upload。
2. `POST /tickets/{id}/messages`，內容包含文字和附件 id。
3. bot 加密後寫入 `ticket_message`（`source=web`、`delivery_status=pending`），更新作者的快照（8.6）與 `ticket.last_message_at`，並放入 worker 工作。
4. worker 用這個頻道的 webhook 送出訊息，名稱和頭像使用該使用者的 Discord 名稱和頭像。**附件以分享連結（8.4.1）的形式附在訊息裡**，不上傳到 Discord，所以不受 Discord 的上傳大小限制；Discord 會自己抓取連結，顯示圖片預覽。
5. 送出成功：存下 `discord_message_id`，狀態改成 `sent`。失敗：由 worker 重試。
6. **同一張客服單的訊息必須依照順序送出**：工作的參數是**客服單**，不是單一訊息（`deliver_messages(ticket_id)`）。worker 執行時：
   1. 取得這張客服單的鎖（Redis lock），同一時間只有一個 worker 處理這張單。
   2. 依照 id 順序，找出這張單所有 `delivery_status=pending` 的訊息，一則一則送出。
   3. 遇到附件還在檢查中的訊息，就停下來稍後重試，後面的訊息也不會插隊。
   - 使用者連續送出三則訊息時，第一個工作就會依序送完三則；後面兩個工作發現沒有待送的訊息，直接結束。已經是 `sent` 的訊息不會重送。

### 8.3 附件規則

- 大小上限：預設 **1 GB**，可以在網頁修改。
- 允許的類型：預設為常見類型（圖片、影片、文件、壓縮檔），可以在網頁自訂。
  - 檢查**副檔名**，加上**檔案開頭的 magic bytes**（用 `filetype` 或 `puremagic`，這兩個都是純 Python，不需要安裝系統套件）。
  - Discord → S3：下載時就讀取開頭檢查，不符合的不上傳，記錄為 `rejected`。
  - 網頁 → S3：申請上傳網址時先檢查副檔名與大小；上傳完成後，worker 讀取物件開頭的幾個 bytes 再檢查一次，不符合就刪除並標記為 `rejected`。
- **大檔案一律使用串流**：從 Discord 下載時一邊下載一邊用 multipart upload 上傳到 S3，不會把整個檔案讀進記憶體。

### 8.4 S3 相容儲存

- 只依賴 S3 API，Cloudflare R2、AWS S3、Silo（MinIO 的社群分支）等都可以使用。端點與憑證都由環境變數設定。
- 使用 **private bucket**，因為客服單裡可能有敏感的截圖。bucket 不公開，所有存取都透過 presigned URL。
- 物件 key：附件是 `tickets/{ticket_id}/{attachment_id}/{filename}`，頭像是 `avatars/{user_id}/{avatar_hash}.{副檔名}`（8.6）。檔名由使用者決定，要先清理（移除路徑分隔符號、控制字元，並限制長度）；原始檔名另外存在資料庫，下載時用 `Content-Disposition` 還原。
- bot 使用的 S3 金鑰只能存取這個 bucket。

**presigned URL**：在網址裡附上簽章與有效期限的 S3 網址（AWS Signature Version 4，S3 相容的服務都支援）。bot 用自己的金鑰在本機算出簽章，不需要呼叫 S3；拿到網址的人可以在期限內直接向 S3 下載，檔案不經過 bot。

- 有效期限預設 **10 分鐘**（`S3_PRESIGN_TTL`）。期限是簽章的一部分，無法延長；需要時直接再產生一個新的（只是本機計算，成本幾乎是零）。
- S3 只在**每個請求開始時**檢查期限，所以下載大檔案的途中過期也不會中斷；但之後要續傳（Range 請求）時，就要用新的網址。
- **影片要特別注意**：瀏覽器播放影片時，會在播放、拖動進度條的過程中不斷送出新的 Range 請求，每個請求都會檢查期限。所以影片播放超過有效期限後，拖動進度條或繼續緩衝就會失敗。前端在播放器發生錯誤時，要重新取得網址並從原本的位置繼續播放。
- 網頁查看附件：`GET /attachments/{attachment_id}` 確認權限後，每次都回傳一個新的 presigned URL。

#### 8.4.1 分享連結（讓 Discord 能顯示附件）

Discord 會自己抓取訊息裡的網址並顯示預覽，但抓取時**不會帶 cookie，也不會登入**；而 presigned URL 最多 7 天就會失效，直接放進 Discord 的話，聊天紀錄裡的附件之後都會壞掉。所以每個附件另外產生一個**分享連結**：

```
GET /a/<attachment_id>/<token>
```

1. 送出網頁的訊息時，為每個附件產生 32 bytes 的隨機 token，資料庫只存 token 的 **SHA-256**。網址由 `PUBLIC_BASE_URL` 加上路徑組成。Discord 傳來的附件本來就在 Discord 裡，不需要分享連結。
2. 有人打開這個網址（包括 Discord 抓取預覽時），bot 比對 token（使用固定時間的比對），確認附件狀態是 `done`、沒有被撤銷。
3. 回傳 `302`，轉址到一個**新的** presigned URL（有效期限同上）。檔案由 S3 直接傳給對方，bot 不經手。轉址的回應要加上 `Cache-Control: no-store`。
4. 圖片、影片使用 `Content-Disposition: inline`（Discord 才會顯示預覽），其他檔案使用 `attachment`。

- **不需要登入**：拿到連結的人就能打開。連結只會出現在私人的客服單頻道裡，而且無法被猜到。
- **可以撤銷**：把 `share_revoked_at` 設定時間，連結就立刻失效（例如附件被判定為不合法時；**關單時也會撤銷這張單所有的分享連結**，見第 10 節）。Discord 已經快取的預覽不會因此消失。
- **客服單開啟期間不會過期**：每次打開都會轉址到新的 presigned URL，聊天紀錄裡的附件不會壞掉。
- 這個 endpoint 要加上速率限制。
- **要實測**：Discord 抓取預覽時是否會跟著 `302` 轉到 S3 的網域。不會的話，改成只有在 Discord 抓取時（User-Agent 含 `Discordbot`），由 bot 直接串流檔案。

### 8.5 SSE

- `GET /tickets/{id}/events`：單一客服單的事件。
- `GET /events`：客服單列表的變動（新開單、被接手、已關閉）。
- 事件類型：`message.created`、`message.updated`、`message.deleted`、`ticket.created`、`ticket.claimed`、`ticket.unclaimed`、`ticket.transferred`、`ticket.member_added`、`ticket.member_removed`、`ticket.opener_added`（開單者加入伺服器後被加進頻道）、`ticket.inactivity_reminded`、`ticket.closed`、`ticket.rated`。
- **所有 SSE 事件都先寫入 `ticket_event`，再推送**（包括 `message.created` 這類訊息事件，`data` 只放 id）。事件的 `id` 就是 `ticket_event.id`。重新連線時，瀏覽器會自動送出 `Last-Event-ID`，伺服器從 `ticket_event` 補送之後的事件（只補送這個使用者有權限看的客服單）。
- 每 25 秒送一次 heartbeat（SSE 註解行），避免 proxy 因為閒置而中斷連線。
- worker 產生的事件（例如自動關閉）透過 Redis pub/sub 通知 bot 服務，再由 bot 推送給瀏覽器。

### 8.6 使用者快照（名稱與頭像）

紀錄頁面要顯示**當時**的名稱與頭像，不能只存 Discord ID：使用者之後可能改名、換頭像，甚至刪除帳號。

- 每張客服單裡**出現過的每個人**（開單者、傳訊息的人、接手、轉交、關單、加入或移除成員的人，以及被加入的成員），在 `ticket_user` 各存一筆快照。
- 這個人每次在客服單裡有動作時，就更新他的快照；**客服單關閉後就不再更新**，保留關單當時的樣子。
- 所有存 Discord ID 的欄位（`opener_id`、`claimed_by_id`、`author_id`、`actor_id` 等），都透過 `(ticket_id, user_id)` 對應到 `ticket_user`。
- 名稱存三種：`username`（帳號名稱）、`global_name`（顯示名稱）、`nickname`（伺服器暱稱）。顯示時依照 Discord 的規則：伺服器暱稱 → 顯示名稱 → 帳號名稱。
- 另外存最高的**分隔顯示身份組**的名稱與顏色，紀錄頁面可以像 Discord 一樣用顏色顯示名稱。
- 開單者不在伺服器裡時，用 Discord REST API 取得帳號資料；沒有伺服器暱稱和身份組。

**頭像要另外存到 S3**：Discord 的頭像網址只在使用者沒有換頭像時有效，換掉之後，舊頭像遲早會無法存取。

- 使用伺服器頭像（有設定的話），否則使用帳號頭像，也就是 discord.py 的 `display_avatar`。
- 伺服器頭像和帳號頭像在 Discord 上的網址格式不同，只存 hash 組不出原本的網址，所以快照另外存 Discord 的原始網址（`discord_avatar_url`）。
- 由 worker 下載後存到 S3：`avatars/{user_id}/{avatar_hash}.{webp 或 gif}`（動態頭像存 gif）。
- **用頭像的 hash 去重**：同一個頭像只下載一次。已經存在 S3 的就略過，所以同一個人開很多張單，也只會存一份。
- API 回傳使用者資料時，頭像依照下面的順序決定：
  1. `avatar_key` 有值：S3 的 presigned URL。
  2. `avatar_hash` 有值，但頭像還沒存到 S3（`mirror_avatar` 還沒執行完）：先用 `discord_avatar_url`。
  3. 都沒有：使用者沒有自訂頭像，前端依照 Discord 的規則顯示預設頭像。

## 9. 閒置提醒與自動關閉

| 時間 | 動作 |
|---|---|
| 最後一則訊息後 **4 天** | 在頻道送出提醒 embed，附上「立即關閉」按鈕 |
| 提醒後再 **1 天**仍然沒有新訊息 | 自動關閉 |

- 兩個時間都可以在網頁修改（`inactivity_remind_after_hours`、`inactivity_close_after_hours`），設為空值就停用。
- 計算的是**使用者或客服**的訊息。bot 自己送出的提醒、系統訊息不算在內。
- 有新訊息時，清除 `inactivity_reminded_at`，重新開始計算。
- 由 scheduler 每 5 分鐘放入一次 `check_inactive_tickets` 工作，worker 掃描資料庫後送出提醒或關單。這種做法不需要在每次有新訊息時取消、重新排程延遲的工作。

## 10. 關單、紀錄與評分

1. 更新 `status=closed`、`closed_at`、`closed_by_id`、`close_reason`（自動關閉時記錄為系統）。
2. 送出 SSE 事件 `ticket.closed`，並在 log 頻道送出通知（如果有設定）。
3. 刪除頻道，webhook 會跟著被刪除。同時**撤銷這張客服單所有的分享連結**（設定 `share_revoked_at`）：頻道刪除後已經沒有人需要它們，就算連結事先被複製出去，關單後也打不開。紀錄頁面不受影響，網頁看附件是用需要登入的 `GET /attachments/{attachment_id}`。
4. worker 送出私訊：
   - **開單者**：紀錄頁面的連結 + 「評分」按鈕。
   - **接手的人**（如果有）：紀錄頁面的連結。
   - 對方關閉私訊、或不在伺服器裡時，在 `ticket_event` 記錄送出失敗，不重試。這種情況下，使用者在網頁上一樣可以看到紀錄並評分。
5. 紀錄頁面：前端透過 `GET /tickets/{id}`、`GET /tickets/{id}/messages` 取得資料後顯示，bot 不產生 HTML。
6. **已關閉的客服單不能重新開啟**，需要時請開新單。

### 10.1 評分

- 按下私訊裡的「評分」按鈕，跳出 Modal，裡面有兩個欄位：
  - **分數**：短文字，必填，只接受 `1`～`5`。送出時驗證，不符合就回覆錯誤訊息，請使用者重新評分。
  - **評語**：長文字，選填。
- 網頁也可以透過 `PUT /tickets/{id}/rating` 評分，內容相同。
- 每張客服單只能評分一次。已經評分過的話，再按私訊裡的按鈕，會回覆「已經評分過了」。
- 之後可以考慮：Discord 已經支援在 Modal 裡放下拉選單，確認 discord.py 支援之後，分數可以改成下拉選單，就不需要另外驗證。

## 11. 背景工作（queue）

使用 **Redis Streams 作為 queue**，搭配 Python 的 task queue 套件 **taskiq**。主要 API 也是 Python（FastAPI），之後要共用 queue 時，同樣使用 taskiq 即可。

- **一個 Redis 可以有很多個 queue**：每個 queue 就只是一個 key（例如 `ticket-bot:queue`、`main-api:queue`），用 queue 的名稱區分，不要用 Redis 的 db 編號（`/0`、`/3`）區分：
  - Redis Cluster 只支援 db 0。
  - **pub/sub 不分 db**：不同 db 裡的程式，訂閱同名的頻道會互相收到訊息。
  - Redis ACL 的權限是依照 key 的前綴設定，跟 db 無關。
- 每個服務各自使用自己的 queue 名稱與 key 前綴（這個 bot 是 `ticket-bot:`），並用 ACL 限制只能存取自己的前綴。唯一的例外是 taskiq-redis 內部用來當作鎖的 `autoclaim:ticket-bot:worker:ticket-bot:queue`，ACL 要一併允許。
- 之後如果有**不是 Python** 的服務要使用 queue，該服務與 bot 之間的工作改用自己定義、有文件的 JSON 格式（例如 `{"type": "send_dm", ...}`），不要讓其他語言去模仿 taskiq 的內部格式。
- **queue 要放在單一主節點的 Redis**，不要放在多主架構的 session store 上。多主架構的複製是非同步的，兩個節點可能同時把同一個工作交給不同的 worker，鎖（Redis lock）也會失效。

### 11.1 工作清單

| 工作 | 由誰放入 |
|---|---|
| `mirror_attachment(attachment_id)`：從 Discord 串流下載，再上傳到 S3 | bot（`on_message`） |
| `verify_attachment(attachment_id)`：檢查網頁上傳的檔案類型 | bot（API 收到上傳完成） |
| `deliver_messages(ticket_id)`：依照順序把網頁的訊息透過 webhook 送到 Discord | bot（API） |
| `mirror_avatar(ticket_id, user_id)`：讀取快照，從 `discord_avatar_url` 下載頭像存到 S3，再寫入 `avatar_key`（S3 上已經有同一個 hash 的檔案就不下載） | 更新使用者快照時，頭像有變的話 |
| `send_close_notifications(ticket_id)`：關單後的私訊（連結、評分） | 關單的流程（bot 或 worker） |
| `check_inactive_tickets()`：閒置提醒、自動關閉 | scheduler（每 5 分鐘） |
| `sync_pending_openers()`：把已經加入伺服器的開單者加進頻道（補 `on_member_join` 錯過的事件） | scheduler（每 5 分鐘） |

### 11.2 可靠性規則

- **資料庫才是唯一的真實狀態**。工作只帶 id（例如 `attachment_id`），worker 執行時再從資料庫讀取最新的資料。
- **每個工作都必須冪等**：執行兩次的結果跟一次一樣。例如送出前先檢查 `delivery_status` 是不是已經 `sent`。
- **使用支援 ack 的 broker**（taskiq-redis 的 Redis Streams broker），確保 worker 中途被停止時，工作會重新被取出，而不是遺失。
  - 工作執行完才 ack。超過 30 分鐘沒有 ack 的工作（例如 worker 當機），會被其他 worker 接手重新執行，所以這個時間要比最長的工作還長；可能超過的工作要先取得鎖，避免同時執行兩次。
  - worker 一次只從 queue 取一個工作。已經取出但還沒開始執行的工作，在 worker 停止後也要等 30 分鐘才會被接手。
- 失敗時使用指數退避重試：間隔 1、2、4、8、16 分鐘，最多執行 6 次。重試是先存進 Redis（`ticket-bot:schedule:`），時間到了由 scheduler 放回 queue，所以 scheduler 停止期間不會重試，重新啟動後會補上。超過次數上限就標記為 `failed` 並記錄。

### 11.3 部署

- bot、worker、scheduler 是三個服務，使用同一個 image。
- **優雅關閉**：部署新版本時，部署平台通常會先送出 `SIGTERM`，等待一段時間後才強制停止。worker 收到 `SIGTERM` 時要停止接新工作，並等目前的工作完成。平台的等待時間要設得比最長的工作還長（例如上傳 1 GB 的檔案）。
- **bot 服務同一時間只能有一個在執行**：如果滾動更新先啟動新版本、再停止舊版本，會有一段時間兩個 bot 同時連著 Discord，事件會被處理兩次（例如訊息被存兩次）。bot 服務要設定成「先停止舊的，再啟動新的」。worker 可以同時有多個。
- scheduler 同一時間也只能有一個。

## 12. 加密

訊息內容在**應用程式層**加密後才存進資料庫。這樣即使資料庫或備份外洩，也看不到內容。

- **演算法：`cryptography` 套件的 MultiFernet**（Fernet = AES-128-CBC + HMAC-SHA256）。
  - 不需要自己處理 nonce 和金鑰版本，最不容易用錯。
  - 內建金鑰輪替：新金鑰放在第一個，舊金鑰還能解密，`rotate()` 可以把舊資料重新加密。
  - AES-128 目前沒有任何實際可行的破解方法。AES-256 多出來的安全餘裕，主要是為了法規要求或對抗未來的量子電腦，這個專案用不到。
  - 沒有選 AES-256-GCM 的原因：它要自己產生 nonce、自己設計金鑰版本的格式，寫錯就會不安全；而它的主要優點（密文比較短、可以綁定額外的驗證資料）在這個專案裡用處不大。
- **加密的欄位**：`ticket.subject`、`ticket.content`、`ticket.close_reason`、`ticket.feedback`、`ticket.webhook_token`、`ticket_message.content`。`webhook_token` 外洩的話，任何人都能用 bot 的 webhook 在客服單頻道發訊息，所以也要加密。
- **實作**：寫一個 SQLAlchemy `TypeDecorator`（`EncryptedText`），讀寫時自動加解密，其他程式碼不需要知道有加密。
- **金鑰**：只放在環境變數 `ENCRYPTION_KEYS`（逗號分隔，第一個是目前使用的金鑰），**不可以存進資料庫**。金鑰遺失，資料就無法還原，一定要另外備份。
- **代價**：加密後的欄位無法在資料庫裡搜尋，也無法排序。要搜尋內容的話，只能另外建立索引。
- **附件與頭像**：使用 S3 的伺服器端加密（SSE），應用程式層不另外加密。
- **使用者名稱（`ticket_user`）不加密**：名稱與頭像本來就是 Discord 上公開的資料，不加密才能用名稱搜尋客服單。

## 13. 資料表

所有 Discord ID 都是 `BigInteger`，所有時間都是 UTC。標示 🔒 的欄位會加密。所有存使用者 Discord ID 的欄位，名稱與頭像都在 `ticket_user`（8.6）。

```
settings（只有一筆，id 固定為 1）
  panel_channel_id, panel_message_id, log_channel_id
  default_discord_category_id, channel_name_template
  max_open_tickets_per_user（預設 3）
  inactivity_remind_after_hours（預設 96）, inactivity_close_after_hours（預設 24）
  max_attachment_size（預設 1 GB）, allowed_file_types（JSON）
  updated_at

ticket_category
  id, name, emoji, description, position, enabled
  discord_category_id（null 表示使用預設）
  created_at, updated_at

permission_role
  id, role_id, kind（admin | category）, category_id（kind=category 時才有值）
  唯一：(role_id, kind, category_id)

ticket
  id（自增，也用在頻道名稱）, category_id, status（open | closed | failed）, source（discord | web）
  channel_id, webhook_id, 🔒webhook_token
  opener_id, opener_added_at（null 表示開單者還沒加進頻道）, 🔒subject, 🔒content
  claimed_by_id, claimed_at
  last_message_at, inactivity_reminded_at, created_at
  closed_at, closed_by_id（null 表示系統自動關閉）, 🔒close_reason
  rating（1–5）, 🔒feedback, rated_at

ticket_user（客服單裡每個人的快照，見 8.6）
  ticket_id, user_id
  username, global_name, nickname
  avatar_hash（null 表示使用預設頭像）, discord_avatar_url（Discord 的原始網址）
  avatar_key（S3 的 key，null 表示還沒存到 S3）
  role_name, role_color（最高的分隔顯示身份組）
  is_bot, updated_at
  主鍵：(ticket_id, user_id)

ticket_member（額外加入的成員）
  ticket_id, user_id, added_by_id, added_at

ticket_message
  id, ticket_id, source（discord | web）, discord_message_id
  author_id
  🔒content, created_at, edited_at, deleted_at
  delivery_status（網頁訊息才有：pending | sent | failed）, attempts

ticket_attachment
  id, ticket_id, message_id（網頁上傳時，送出訊息前是 null）, uploader_id
  source（discord | web）, discord_attachment_id（worker 用來重新取得 Discord 的下載網址）, storage_key
  filename, content_type, size
  status（pending | done | rejected | failed）, attempts
  share_token_hash（分享連結 token 的 SHA-256，只有網頁上傳的附件才有）, share_revoked_at
  created_at

ticket_event（時間軸、稽核紀錄；所有 SSE 事件都會寫在這裡）
  id, ticket_id, type, actor_id（null 表示系統）, data（JSON）, created_at
```

主要的索引：

- `ticket(opener_id, status)`：檢查同時開單的上限。
- `ticket(channel_id)`（唯一）：按鈕和 `on_message` 用頻道查客服單。
- `ticket(status, last_message_at)`：掃描閒置的客服單。
- `ticket(status, opener_added_at)`：找出開單者還沒加進頻道的客服單。
- `ticket_message(ticket_id, id)`、`ticket_event(ticket_id, id)`：依照順序分頁讀取、SSE 補送。

## 14. API

- bot 有自己的網域，endpoint 直接放在根路徑底下。
- 除了 `GET /health`、`POST /auth/exchange`、`GET /a/{attachment_id}/{token}` 以外，所有 endpoint 都需要登入（bot 的 cookie），權限依照第 4 節判斷。
- Discord ID 在 JSON 裡一律用**字串**，因為 snowflake 超過 JavaScript 的安全整數範圍。
- 列表類的 endpoint（客服單、訊息）使用 cursor 分頁（`?before=<id>&limit=50`），不用 offset：訊息一直在增加，用 offset 會漏掉或重複。

| 分類 | Endpoint | 權限 |
|---|---|---|
| 健康檢查 | `GET /health` | 不需要 |
| 登入 | `POST /auth/exchange`（用授權碼換 cookie）、`POST /auth/logout` | 見 `docs/auth.md` |
| 身分 | `GET /me`：Discord ID，以及是不是管理員、負責哪些分類 | 登入 |
| Discord 資料 | `GET /guild/channels`、`GET /guild/roles` | 管理員 |
| 身份組 | `POST /guild/roles`（在 Discord 建立）、`PATCH`、`DELETE /guild/roles/{role_id}` | 管理員 |
| 權限身份組 | `GET`、`PUT /permission-roles`（整份取代：`admin_role_ids`、每個分類的 `role_ids`） | 管理員 |
| 設定 | `GET`、`PATCH /settings` | 管理員 |
| 分類 | `GET`、`POST /categories`，`PATCH`、`DELETE /categories/{category_id}`（已經有客服單的分類不能刪除，回傳 `409`，只能停用） | 管理員（`GET` 登入就可以，但只看得到啟用中的分類） |
| 面板 | `POST /panel/publish` | 管理員 |
| 客服單 | `POST /tickets`（從網頁開單）、`GET /tickets`（篩選、分頁）、`GET /tickets/{id}` | 依照權限 |
| 訊息 | `GET`、`POST /tickets/{id}/messages` | 依照權限 |
| 附件 | `POST /tickets/{id}/attachments`（取得上傳網址）、`POST /attachments/{attachment_id}/complete`、`GET /attachments/{attachment_id}`（取得新的 presigned URL） | 依照權限 |
| 分享連結 | `GET /a/{attachment_id}/{token}`（302 轉址到新的 presigned URL，見 8.4.1） | 不需要登入，用 token 驗證 |
| 操作 | `POST /tickets/{id}/claim`、`/unclaim`、`/transfer`、`/close`、`/members`，`DELETE /tickets/{id}/members/{user_id}` | 依照權限 |
| 評分 | `PUT /tickets/{id}/rating`（分數 1–5、評語） | 開單者，只能一次 |
| 即時事件 | `GET /events`、`GET /tickets/{id}/events`（SSE） | 依照權限 |

FastAPI 會自動產生 OpenAPI 文件（`/docs`），給前端開發時參考。

## 15. 已做的決定

| 項目 | 決定 | 原因 |
|---|---|---|
| 客服單的形式 | 私人頻道 | 權限彈性，使用者也熟悉 |
| 關單後 | 刪除頻道 | 紀錄都在資料庫，由網頁顯示 |
| 訊息保存 | 每則訊息送出時就存進資料庫，內容加密 | 網頁要即時同步；紀錄頁面直接使用 |
| 名稱與頭像 | 每張客服單保存每個人的快照，關單後不再更新；頭像存到 S3 | 使用者改名、換頭像或刪除帳號後，紀錄頁面仍然顯示當時的樣子 |
| 紀錄頁面 | 由前端用結構化的資料產生 | 之後改版型、搜尋、控制權限、評分都比較容易；需要時再加「匯出成 HTML 檔」 |
| ORM / migration | SQLAlchemy 2.0 async + Alembic | 一套程式碼支援三種資料庫 |
| 即時事件 | SSE | 瀏覽器只需要接收；會自動重新連線；可以帶 cookie |
| 附件儲存 | S3 相容 API，private bucket，presigned URL 預設 10 分鐘 | 不綁定特定的服務商；檔案不經過 bot |
| Discord 裡的附件 | 分享連結（隨機 token，302 轉址到新的 presigned URL），關單時撤銷 | Discord 能顯示預覽、客服單開啟期間連結不會過期、可以撤銷，bot 也不用中轉檔案 |
| 背景工作 | Redis Streams + taskiq，獨立的 worker；用 queue 名稱與 key 前綴區分服務 | 部署新版本時不會中斷工作；主要 API 也是 Python，可以共用 |
| API 框架 | FastAPI，跟 discord.py 在同一個程序 | 可以直接操作 bot；自動產生 OpenAPI 文件 |
| 身分驗證 | 背景交換授權碼，bot 有自己的 cookie，並直接查詢 session store | cookie 不共用；瀏覽器直接連到 bot；登出立刻生效（見 `docs/auth.md`） |
| 開單者不在伺服器 | 照常開單，等開單者加入伺服器後再加進頻道 | 被停權的使用者可以從網頁開單申請解除停權 |
| 加密 | MultiFernet | 最不容易用錯，內建金鑰輪替 |
| 部署位置 | bot、worker、資料庫、Redis、session store 的節點都在同一個地區 | 資料庫查詢不能跨洲 |
| 開單上限 | 每人同時 3 張 | |
| 閒置處理 | 4 天提醒，再 1 天自動關閉 | |
| 附件大小 | 1 GB | 與 Discord Nitro 的上傳上限相同 |
| 成員管理 | 開單者、分類身份組、管理員都可以加入、移除成員；對象不能是開單者或客服 | |
| 轉交 | 可以換人，不能換分類 | |
| 重新開啟 | 不支援 | 頻道已經刪除，直接開新單 |
| 評分 | 私訊按鈕 → Modal（分數 1–5、評語），只能一次 | |

## 16. 環境變數（初步）

```
DISCORD_TOKEN
GUILD_ID
DATABASE_URL          例：sqlite+aiosqlite:///data/bot.db
                          mysql+aiomysql://user:pass@host/db?charset=utf8mb4
                          postgresql+asyncpg://user:pass@host/db
REDIS_URL             queue、事件通知、鎖、ticket session 共用（單一主節點，與 session store 分開）
ENCRYPTION_KEYS       Fernet 金鑰，逗號分隔，第一個是目前使用的金鑰
S3_ENDPOINT, S3_REGION, S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY, S3_BUCKET
S3_PRESIGN_TTL        presigned URL 的有效期限（秒，預設 600）
PUBLIC_BASE_URL       bot 對外的網址，用來組出分享連結（8.4.1）
WEB_TICKET_URL        紀錄頁面網址的格式，例：https://.../tickets/{id}
```

API 與身分驗證相關的環境變數見 `docs/auth.md`。

## 17. 需要實測的項目

- Discord 抓取附件預覽時，是否會跟著 `302` 轉址（8.4.1）。
