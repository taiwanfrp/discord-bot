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
- **轉交**：只能轉給有該分類身份組或管理員身份組的人，**不能換分類**。管理員也可以把還沒有人接手的單直接指派給某個客服。
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
| `@everyone` | 禁止查看、禁止建立討論串（討論串裡的訊息不會同步到網頁） |
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
- 按鈕或指令造成的變更（接手、釋放、轉交、加入與移除成員）都會在頻道裡送出一則說明；加入成員與轉交會通知對方。
- 客服單頻道在 Discord 上被直接刪除時，客服單會被標記為關閉，關閉者是系統。
- **私訊裡的評分按鈕不在客服單頻道裡**，無法用 `channel_id` 查詢，而且頻道也已經刪除了。所以評分按鈕要把客服單 id 放進 `custom_id`（例如 `ticket:rate:12`），使用 discord.py 的 `DynamicItem` 解析，bot 重啟後一樣能使用。
- slash command：`/ticket add`、`/ticket remove`（加入、移除成員）、`/ticket transfer`（轉交）、`/ticket close [reason]`。

## 8. 訊息同步

**每一則訊息在送出的當下就寫進資料庫**，不是等到關單才保存。所以紀錄頁面隨時都是最新的，就算頻道被誤刪，紀錄也不會遺失。

### 8.1 Discord → 網頁

1. `on_message`：只處理客服單頻道裡的訊息，並略過：
   - `message.webhook_id` 是這張客服單自己的 webhook 的訊息（那是從網頁送出的訊息，已經存過了）。
   - bot 自己送出的訊息（開單的 embed、閒置提醒等）。這些動作記錄在 `ticket_event`，不算是對話內容。
   - 系統訊息（釘選、加入討論串等），只處理一般訊息與回覆。
   - bot 需要 **Message Content** 這個特權 intent（在 Developer Portal 開啟），否則收到的訊息內容與附件都是空的。
2. 加密後存進 `ticket_message`，更新作者的快照（8.6）、`ticket.last_message_at`，並清除 `inactivity_reminded_at`（第 9 節）。同一則訊息重複收到時（`discord_message_id` 是唯一的）什麼都不做。
3. 附件：每個附件存一筆 `ticket_attachment`，同時存下 Discord 的下載網址（`source_url`）。**一定要上傳到 S3**，因為 Discord 的附件網址大約一天就會過期。
   - 副檔名或大小不符合（8.3）：直接記錄為 `rejected`，不放入工作。
   - 符合：記錄為 `pending`，放入 worker 工作 `mirror_attachment`（下載後上傳到 S3）。
   - worker 先用 `source_url` 下載；網址已經過期（`403`、`404`）時，才透過 REST API 重新取得訊息，拿到新的網址。訊息或頻道已經被刪除的話，記錄為 `failed`。所以傳了附件後馬上關單、頻道被刪除，附件也還來得及存下來。
   - 同一個附件用鎖確保只有一個 worker 處理。取不到鎖時（另一個 worker 正在處理，或是它當機了、鎖還沒過期）丟出例外，讓 queue 稍後重試。
   - 最後一次重試也失敗時記錄為 `failed`。
   - 放入工作失敗的附件（例如當時 Redis 連不上），由 scheduler 每 10 分鐘執行的 `requeue_pending_attachments` 重新放入。
4. 送出 SSE 事件 `message.created`。附件處理完（`done`、`rejected`、`failed`）時送出 `attachment.updated`，網頁更新附件的顯示。
5. 編輯、刪除：用 `on_raw_message_edit`、`on_raw_message_delete`、`on_raw_bulk_message_delete` 更新資料庫。一般的 `on_message_edit`、`on_message_delete` 只收得到快取裡的訊息，bot 重啟之前的訊息就收不到。
   - 編輯：舊的內容存進 `ticket_message_revision`（加密，一筆是一個舊版本，記錄這一版從什麼時候到什麼時候），`ticket_message` 改成新的內容並設定 `edited_at`，送出 `message.updated`。訊息的 id 不變，網頁的排序、翻頁都不受影響。
     - 內容和附件都沒變的更新不算編輯，例如 Discord 產生連結預覽時。
     - 編輯時移除的附件：檔案保留，設定 `removed_at`。
     - 只處理從 Discord 傳的訊息。網頁送出的訊息只有 bot 自己的 webhook 能編輯，而且 Discord 上的內容多了附件連結，本來就跟資料庫裡的不一樣。
   - 刪除：只設定 `deleted_at`，內容不清空，送出 `message.deleted`。網頁送出的訊息在 Discord 上被刪除（例如客服刪掉）也一樣，並撤銷它的附件的分享連結。Discord 不會告訴 bot 是誰刪的，所以事件沒有 `actor_id`。
   - **網頁上只有這張單的客服、管理員看得到原本的內容**：編輯前的每一版、被刪除的訊息、編輯時移除的附件。開單者和額外加入的成員只看到「已編輯」「已刪除」的標記，跟在 Discord 上看到的一樣，例如使用者貼錯密碼後刪掉，其他成員就看不到。
   - 新增、編輯、刪除依照收到的順序處理（bot 裡的 `asyncio.Lock`）。discord.py 每個事件各自一個 task，訊息傳出後馬上被其他 bot 刪除時，刪除可能比新增先處理完，就找不到要刪除的訊息。bot 同一時間只有一個（11.3），所以不需要 Redis 鎖。
   - bot 停止期間（例如部署時）的編輯、刪除收不到，Discord 不會補送。關單後頻道已經刪除，也不再更新。
   - 網頁上的訊息還不能編輯、刪除。

### 8.2 網頁 → Discord

1. **上傳附件**：瀏覽器用 presigned URL **直接上傳到 S3，不經過 bot**。
   1. `POST /tickets/{id}/attachments`（檔名、大小）：bot 先檢查副檔名與大小（8.3），回傳上傳的方式：
      - 不超過 8 MiB：一個上傳網址，瀏覽器直接 `PUT` 整個檔案。
      - 超過：multipart upload。每一塊都是 8 MiB（只有最後一塊比較小），用 `POST /attachments/{id}/parts` 取得每一塊的上傳網址。網址有期限（`S3_PRESIGN_TTL`），所以前端在上傳之前才分批取得，一次最多 100 塊。
      - 網址的簽章包含檔案（或每一塊）的大小，上傳的大小不同時 S3 會拒絕。
   2. 上傳完呼叫 `POST /attachments/{id}/complete`。multipart upload 由 bot 用 ListParts 確認每一塊都上傳了、大小正確，再合併成一個檔案；瀏覽器不需要讀取每一塊的 ETag。
   3. bot 放入 `verify_attachment`，worker 檢查檔案內容（8.3），結果用 SSE 的 `attachment.updated` 通知。
   - 還沒送出的附件可以用 `DELETE /attachments/{id}` 移除。同一個人在同一張單裡，最多同時有 10 個還沒送出的附件。
   - 上傳後 24 小時還沒送出的附件（例如使用者關掉網頁），由 scheduler 每小時放入的 `delete_unsent_attachments` 刪除。
   - 申請網址以外的操作，只有上傳的人自己可以做。
2. `POST /tickets/{id}/messages`：文字（最多 2000 字）和附件 id（最多 10 個）。附件要是自己在這張單上傳完成、還沒送出的；還在檢查中的也可以，訊息會等它檢查完再送出。
3. bot 加密後寫入 `ticket_message`（`source=web`、`delivery_status=pending`），更新作者的快照（8.6）與 `ticket.last_message_at`，並放入 `deliver_messages(ticket_id)`。
4. worker 用這個頻道的 webhook 送出訊息，名稱和頭像使用該使用者的 Discord 名稱和頭像。**附件以分享連結（8.4.1）的形式附在訊息裡**，不上傳到 Discord，所以不受 Discord 的上傳大小限制；Discord 會自己抓取連結，顯示圖片預覽。
   - 每個附件一行：`📎 [檔名](分享連結)`。沒有通過檢查的附件只顯示檔名與原因。
   - 文字加上附件連結不能超過 Discord 的 2000 字上限，超過時 API 回傳 `422`，請使用者縮短內容或分開送出。
   - 訊息裡的 `@everyone`、`<@id>` 只顯示，不會通知任何人。
   - Discord 不接受含有 `discord`、`clyde` 的 webhook 名稱，bot 在中間插入間隔號（例如 `Di·scord`）。實測零寬空白、外觀相似的字母（例如西里爾字母 `о`）、全形字母都會被 Discord 當成同一個字。Discord 還是不接受的話，改用「網頁使用者」。
   - 沒有自訂頭像的人使用 Discord 的預設頭像。
   - webhook 被刪除的話（例如管理員在頻道設定裡刪掉），建立新的 webhook 再送一次。
5. 送出成功：存下 `discord_message_id`，狀態改成 `sent`。失敗：由 worker 重試；最後一次重試也失敗，或是 Discord 回傳 `400`（內容有問題，重試也一樣），就標記為 `failed`，繼續送下一則。客服單已經關閉、頻道已經刪除時也標記為 `failed`。狀態改變時送出 SSE 事件 `message.updated`。
6. **同一張客服單的訊息必須依照順序送出**：工作的參數是**客服單**，不是單一訊息（`deliver_messages(ticket_id)`）。worker 執行時：
   1. 取得這張客服單的鎖（Redis lock），同一時間只有一個 worker 處理這張單。另一個 worker 正在送的話，最多等 30 秒，還是取不到就丟出例外，稍後重試。
   2. 依照 id 順序，找出這張單所有 `delivery_status=pending` 的訊息，一則一則送出。
   3. 遇到附件還在檢查中的訊息，就停下來，後面的訊息也不會插隊。附件檢查完之後，`verify_attachment` 會再放入 `deliver_messages`。
   - 使用者連續送出三則訊息時，第一個工作就會依序送完三則；後面兩個工作發現沒有待送的訊息，直接結束。已經是 `sent` 的訊息不會重送。
   - 送出成功、但寫入資料庫之前 worker 中斷的話，重試時會再送一次。Discord 的 webhook 沒有防止重複送出的機制。

### 8.3 附件規則

- 大小上限：預設 **1 GB**，可以在網頁修改。
- 允許的類型：預設為常見類型（圖片、影片、文件、壓縮檔），可以在網頁自訂。
  - 檢查**副檔名**，加上**檔案開頭的 magic bytes**（用 `filetype`，純 Python，不需要安裝系統套件；它最多看檔案開頭的 8192 bytes）。
    - 純文字（txt、csv 等）沒有 magic bytes，偵測不到類型時只依照副檔名。
    - 同一種容器格式不一定分得出來（例如 docx 可能被偵測成 zip）。偵測到的類型跟副檔名屬於同一組（zip、Office、OpenDocument；或舊版的 doc、xls、ppt），而且副檔名被允許時就接受。
  - Discord → S3：下載時就讀取開頭檢查，不符合的不上傳，記錄為 `rejected`。
  - 網頁 → S3：申請上傳網址時先檢查副檔名與大小；上傳完成後，worker 確認實際的大小，並讀取開頭的 8192 bytes 再檢查一次，不符合就刪除並標記為 `rejected`。
  - 網頁上傳的檔案，下載時的 Content-Type 使用 magic bytes 偵測到的類型（偵測不到時是 `application/octet-stream`），不使用瀏覽器送來的類型。
- **大檔案一律使用串流**：從 Discord 下載時一邊下載一邊用 multipart upload 上傳到 S3，不會把整個檔案讀進記憶體。

### 8.4 S3 相容儲存

- 只依賴 S3 API，Cloudflare R2、AWS S3、Silo（MinIO 的社群分支）等都可以使用。端點與憑證都由環境變數設定。
- 使用 **private bucket**，因為客服單裡可能有敏感的截圖。bucket 不公開，所有存取都透過 presigned URL。
- 物件 key：附件是 `tickets/{ticket_id}/{attachment_id}/{filename}`，頭像是 `avatars/{user_id}/{avatar_hash}.{副檔名}`（8.6）。檔名由使用者決定，要先清理（移除路徑分隔符號、控制字元，並限制長度）；原始檔名另外存在資料庫，下載時用 `Content-Disposition` 還原。
- bot 使用的 S3 金鑰只能存取這個 bucket。
- bucket 要設定 lifecycle 規則，自動清除沒有完成的 multipart upload（`AbortIncompleteMultipartUpload`）。worker 上傳到一半被強制停止，或是使用者上傳到一半就離開時，已經上傳的部分會留在 S3 上佔用空間。Silo 不接受這個規則，它用自己的 `stale_uploads_expiry` 設定清除。
- 瀏覽器直接上傳、下載，所以 bucket 要設定 **CORS**：允許前端的網域（`API_CORS_ORIGINS`）使用 `GET`、`HEAD`、`PUT`，`AllowedHeaders` 是 `*`，`ExposeHeaders` 加上 `ETag`（bot 用 ListParts 取得，不需要；前端的上傳套件可能會讀取）。本機開發用 `uv run python -m bot.storage.create_bucket` 設定；正式環境在建立 bucket 時設定。

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

1. worker 送出網頁的訊息時，為每個附件產生 32 bytes 的隨機 token，資料庫只存 token 的 **SHA-256**。網址由 `PUBLIC_BASE_URL` 加上路徑組成。Discord 傳來的附件本來就在 Discord 裡，不需要分享連結。
   - 每次送出都產生新的 token：資料庫只有雜湊，重試時拿不到上一次的 token。
   - token 的雜湊先 commit 再送出訊息，因為 Discord 收到訊息後馬上就會來抓預覽。
2. 有人打開這個網址（包括 Discord 抓取預覽時），bot 比對 token（使用固定時間的比對），確認附件狀態是 `done`、沒有被撤銷。不存在、token 錯誤、已經撤銷都回傳一樣的 `404`。也接受 `HEAD`。
3. 回傳 `302`，轉址到一個**新的** presigned URL（有效期限同上）。檔案由 S3 直接傳給對方，bot 不經手。轉址的回應要加上 `Cache-Control: no-store`。
4. presigned URL 帶上 `response-content-type` 與 `response-content-disposition` 參數，S3 依照這兩個參數回傳 Content-Type 與檔名，不依賴物件本身的設定（不同的 S3 服務處理方式不同，例如實測 AWS S3 會依照副檔名覆蓋上傳時設定的 Content-Type）。
   - 圖片、影片使用 `Content-Disposition: inline`（Discord 才會顯示預覽），其他檔案使用 `attachment`。SVG 裡面可以放 script，所以一律下載。
   - 檔名：`filename*` 是 UTF-8 的原始檔名，`filename` 是給不支援的程式用的 ASCII 檔名。

- **不需要登入**：拿到連結的人就能打開。連結只會出現在私人的客服單頻道裡，而且無法被猜到。
- **可以撤銷**：把 `share_revoked_at` 設定時間，連結就立刻失效（例如附件被判定為不合法時；**關單時也會撤銷這張單所有的分享連結**，見第 10 節）。Discord 已經快取的預覽不會因此消失。
- **客服單開啟期間不會過期**：每次打開都會轉址到新的 presigned URL，聊天紀錄裡的附件不會壞掉。
- **速率限制**：每個 IP 每分鐘 60 次（Redis 計數）。Redis 無法使用時放行，因為這是為了擋大量請求，安全性靠的是 token 本身。
- **實測結果**：Discord 抓取預覽時會跟著 `302` 轉址到 S3 的網域。它依照 Content-Type 或檔案內容判斷類型，網址沒有副檔名也能顯示預覽（用短網址服務的 `302` 轉址到 AWS S3 測試）。webhook 訊息裡用 `[檔名](網址)` 的寫法也會顯示圖片預覽（直接連到圖片的網址測試）。

### 8.5 SSE

- `GET /tickets/{id}/events`：單一客服單的事件。
- `GET /events`：客服單列表的變動（新開單、被接手、已關閉）。
- 事件類型：`message.created`、`message.updated`（網頁訊息送到 Discord 的結果 `delivery_status`，8.2；Discord 上的編輯，8.1）、`message.deleted`（Discord 上的刪除，8.1）、`attachment.updated`（附件處理完，8.1、8.2）、`ticket.created`、`ticket.claimed`、`ticket.unclaimed`、`ticket.transferred`、`ticket.member_added`、`ticket.member_removed`、`ticket.opener_added`（開單者加入伺服器後被加進頻道）、`ticket.inactivity_reminded`、`ticket.closed`、`ticket.rated`、`notification.sent`、`notification.failed`（關單私訊的結果，第 10 節）。`GET /events` 只送 `ticket.*` 的事件。
- **所有 SSE 事件都先寫入 `ticket_event`，再推送**（包括 `message.created` 這類訊息事件，`data` 只放 id）。事件的 `id` 就是 `ticket_event.id`。重新連線時，瀏覽器會自動送出 `Last-Event-ID`，伺服器從 `ticket_event` 補送之後的事件（只補送這個使用者有權限看的客服單）。
  - 最多補送 1000 個事件。斷線太久的話，前端重新連線後應該重新載入資料。
  - 事件的順序不一定跟 id 相同：id 在新增時就決定，兩個交易同時寫入時，比較小的 id 可能比較晚 commit。所以伺服器不會因為 id 比較小就略過事件，前端也可能重複收到同一個事件（重新連線時），要用事件的 id 去重。
- 每 15 秒送一次 heartbeat（SSE 註解行，FastAPI 內建的 SSE 固定是這個間隔），避免 proxy 因為閒置而中斷連線。
- 連線期間每 60 秒重新驗證一次 session 與權限（`docs/auth.md`）。失敗就結束連線，例如已經登出、被移除身份組。
- SSE 連線不會一直佔用資料庫連線：只有補送、驗證、檢查權限時才短暫使用。
- **`ticket_event` commit 之後會自動推送**（`bot/db/hooks.py`）：先透過 Redis pub/sub 通知 bot 服務，再由 bot 推送給瀏覽器。bot 和 worker 產生的事件（例如自動關閉）都走同一條路，不需要每個地方自己通知。

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
- 由 scheduler 每分鐘放入 `mirror_avatars()`，worker 把所有還沒存到 S3 的頭像（`avatar_hash` 有值、`avatar_key` 是空的）一次處理掉，存到 `avatars/{user_id}/{avatar_hash}.{webp 或 gif}`（動態頭像存 gif）。更新快照的地方很多，用定時掃描的方式，就不需要每個地方都記得放入工作。
- **用頭像的 hash 去重**：同一個頭像只下載一次。已經存在 S3 的就略過，所以同一個人開很多張單，也只會存一份。
- 這只是把快照裡的頭像存起來，不會改變快照的內容，所以已關閉的客服單也會處理。
- Discord 上已經取不到的頭像（`403`、`404`）或超過 10 MB 的，清除 `discord_avatar_url`，之後顯示預設頭像。
- API 回傳使用者資料時，頭像依照下面的順序決定：
  1. `avatar_key` 有值：S3 的 presigned URL。
  2. `discord_avatar_url` 有值（頭像還沒存到 S3，最多等 1 分鐘）：先用 Discord 的網址。
  3. 都沒有：使用者沒有自訂頭像，或頭像已經取不到，前端依照 Discord 的規則顯示預設頭像。

## 9. 閒置提醒與自動關閉

| 時間 | 動作 |
|---|---|
| 最後一則訊息後 **4 天** | 在頻道送出提醒 embed，附上「立即關閉」按鈕 |
| 提醒後再 **1 天**仍然沒有新訊息 | 自動關閉 |

- 兩個時間都可以在網頁修改（`inactivity_remind_after_hours`、`inactivity_close_after_hours`），設為空值就停用。
  - 提醒停用時，就不會有新的單被提醒，所以也不會自動關閉；已經提醒過的單，關閉的時間到了照樣關閉。
  - 自動關閉停用時，只提醒，提醒裡也不會寫自動關閉的時間。
- 計算的是**使用者或客服**的訊息（Discord 與網頁都算，`ticket.last_message_at`）。bot 自己送出的提醒、系統訊息不算在內。開單之後完全沒有訊息的話，從開單的時間開始算。
- 有新訊息時，清除 `inactivity_reminded_at`，重新開始計算。
- 由 scheduler 每 5 分鐘放入一次 `check_inactive_tickets` 工作，worker 掃描資料庫後送出提醒或關單。這種做法不需要在每次有新訊息時取消、重新排程延遲的工作。
  - 用鎖確保同一時間只有一個在執行（上一次還沒結束時，這一次直接結束）。每次最多各處理 50 張，剩下的下一次再處理。一張單失敗只記錄錯誤，不影響其他的單。
- **提醒**：在頻道送出 embed（閒置多久、自動關閉的時間），提及開單者（開單者已經加進頻道時）。
  - 「立即關閉」跟開單訊息的「關閉」是同一個按鈕（`custom_id` 相同，由 bot 處理），按下後一樣要確認。
  - **先送出提醒再記錄** `inactivity_reminded_at`，並送出 SSE 事件 `ticket.inactivity_reminded`：記錄之前中斷的話，下一次會再提醒一次，但不會沒有提醒就自動關閉。
  - 記錄時用帶條件的 UPDATE 確認還是沒有新訊息；送出提醒的同時剛好有新訊息的話，刪除這則提醒。
  - 頻道已經不存在（bot 離線時被刪除，錯過了 `on_guild_channel_delete`）：跟頻道被刪除一樣，由系統關單。
- **自動關閉**：跟一般的關單流程相同（第 10 節），關閉者是系統，關閉原因是「提醒後 1 天沒有新訊息，自動關閉」。關單的 UPDATE 再確認一次條件，查詢之後剛好有新訊息的話就不關閉。
- worker 只有 REST API，沒有頻道的快取，所以關單時刪除頻道、送 log 都是快取裡沒有時才向 Discord 取得頻道（`bot/tickets/channels.py`）。

## 10. 關單、紀錄與評分

1. 更新 `status=closed`、`closed_at`、`closed_by_id`、`close_reason`（自動關閉時記錄為系統）。
2. 送出 SSE 事件 `ticket.closed`，並在 log 頻道送出通知（如果有設定；開單時也會通知）。
3. 刪除頻道，webhook 會跟著被刪除。同時**撤銷這張客服單所有的分享連結**（設定 `share_revoked_at`）：頻道刪除後已經沒有人需要它們，就算連結事先被複製出去，關單後也打不開。紀錄頁面不受影響，網頁看附件是用需要登入的 `GET /attachments/{attachment_id}`。
4. 放入 worker 工作 `send_close_notifications`，送出私訊（embed：分類、關閉者、主旨、關閉原因）：
   - **開單者**：「查看紀錄」（紀錄頁面的連結，`WEB_TICKET_URL`）+ 「評分」按鈕。
   - **接手的人**（如果有）：「查看紀錄」。
   - 關閉者用關單當時的快照顯示名稱（私訊裡的 `<@id>` 不一定顯示得出名稱）；系統關閉時顯示「系統」。
   - 每個收件人送出後各記錄一筆 `notification.sent`，重試時略過已經送過的人。送出之後、記錄之前 worker 當機的話，重試會再送一次（至少送一次）。
   - 對方關閉私訊、沒有共同的伺服器、帳號已刪除（Discord 回傳 `4xx`，`429` 除外）時，記錄 `notification.failed`（`data` 有 Discord 的錯誤代碼），不重試。其他錯誤會重試，最後一次也失敗時一樣記錄 `notification.failed`。這種情況下，使用者在網頁上一樣可以看到紀錄並評分。
   - 所有收件人都處理完後，寫入 `ticket.close_notified_at`。放入 queue 失敗的（例如當時 Redis 連不上），由 scheduler 每 10 分鐘執行的 `requeue_close_notifications` 補放：只補關單超過 10 分鐘、一天以內的單，太久以前的私訊已經沒有意義。
   - 同一張單用鎖確保只有一個 worker 在送，取不到鎖時稍後重試。
5. 紀錄頁面：前端透過 `GET /tickets/{id}`、`GET /tickets/{id}/messages` 取得資料後顯示，bot 不產生 HTML。附件用 `download_url`（presigned URL），過期後用 `GET /attachments/{id}` 重新取得。
6. **已關閉的客服單不能重新開啟**，需要時請開新單。

### 10.1 評分

- 按下私訊裡的「評分」按鈕，跳出 Modal，裡面有兩個欄位：
  - **分數**：下拉選單，必填，從 `★★★★★（5 分）` 非常滿意到 `★☆☆☆☆（1 分）` 非常不滿意，高分在上面。只能選這 5 個，不需要另外驗證（discord.py 2.6 起的 `discord.ui.Label`）。
  - **評語**：長文字，選填，最多 1000 字。
- 按鈕的 `custom_id` 是 `ticket:rate:{客服單 id}`（discord.py 的 `DynamicItem`），bot 重新啟動後，舊的私訊按鈕也還能用。
- 評分成功後，私訊的 embed 加上分數（例如 `★★★★☆（4 分）`）與評語，「評分」按鈕會被拿掉，只留下「查看紀錄」。這則私訊只有開單者自己看得到。
- 先在網頁評分的話，私訊裡還留著「評分」按鈕；按下時把私訊改成顯示評分，並回覆「已經評分過了」。這樣不需要記錄私訊的訊息 id。接手的人的私訊不會加上評分，客服在網頁上看。
- 網頁也可以透過 `PUT /tickets/{id}/rating` 評分，內容相同。
- 每張客服單只能評分一次。網頁與 Discord 同時送出時，用帶條件的 UPDATE 確保只有一個成功。
- 評分會送出 SSE 事件 `ticket.rated`。評分與評語只有開單者與這個分類的客服看得到，額外加入的成員看不到。

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
| `requeue_pending_attachments()`：重新放入沒有成功放進 queue 的 `mirror_attachment`、`verify_attachment` | scheduler（每 10 分鐘） |
| `verify_attachment(attachment_id)`：檢查網頁上傳的檔案大小與類型 | bot（API 收到上傳完成） |
| `delete_unsent_attachments()`：刪除上傳後 24 小時還沒送出的附件 | scheduler（每小時） |
| `deliver_messages(ticket_id)`：依照順序把網頁的訊息透過 webhook 送到 Discord | bot（API）、`verify_attachment`（訊息在等的附件檢查完時） |
| `requeue_pending_messages()`：重新放入超過 10 分鐘還沒送出的訊息的 `deliver_messages` | scheduler（每 10 分鐘） |
| `mirror_avatars()`：把還沒存到 S3 的頭像存起來，再寫入 `avatar_key`（S3 上已經有同一個 hash 的檔案就不下載，見 8.6） | scheduler（每分鐘） |
| `send_close_notifications(ticket_id)`：關單後的私訊（連結、評分） | 關單的流程（bot 或 worker） |
| `requeue_close_notifications()`：重新放入一天內關閉、私訊還沒處理完的 `send_close_notifications` | scheduler（每 10 分鐘） |
| `check_inactive_tickets()`：閒置提醒、自動關閉 | scheduler（每 5 分鐘） |
| `sync_pending_openers()`：把已經加入伺服器的開單者加進頻道（補 `on_member_join` 錯過的事件） | scheduler（每 5 分鐘） |
| `sync_ticket_permissions()`：讓開啟中客服單頻道的身份組權限覆寫，跟權限身份組的設定一致 | bot（API 修改權限身份組後） |

### 11.2 可靠性規則

- **資料庫才是唯一的真實狀態**。工作只帶 id（例如 `attachment_id`），worker 執行時再從資料庫讀取最新的資料。
- **每個工作都必須冪等**：執行兩次的結果跟一次一樣。例如送出前先檢查 `delivery_status` 是不是已經 `sent`。
- **使用支援 ack 的 broker**（taskiq-redis 的 Redis Streams broker），確保 worker 中途被停止時，工作會重新被取出，而不是遺失。
  - 工作執行完才 ack。超過 30 分鐘沒有 ack 的工作（例如 worker 當機），會被其他 worker 接手重新執行，所以這個時間要比最長的工作還長；可能超過的工作要先取得鎖，避免同時執行兩次。
  - worker 一次只從 queue 取一個工作。已經取出但還沒開始執行的工作，在 worker 停止後也要等 30 分鐘才會被接手。
- 失敗時使用指數退避重試：間隔 1、2、4、8、16 分鐘，最多執行 6 次。重試是先存進 Redis（`ticket-bot:schedule:`），時間到了由 scheduler 放回 queue，所以 scheduler 停止期間不會重試，重新啟動後會補上。超過次數上限就標記為 `failed` 並記錄。

### 11.3 部署

bot、worker、scheduler 三個服務使用同一個 image（`Dockerfile`），只有啟動指令不同。image 的工作目錄是 `/app`，用非 root 的使用者執行。設定全部來自環境變數，image 裡沒有 `.env`。

建立 image：`docker build -t taiwanfrp-discord-bot .`。放進 image 的檔案由 `.dockerignore` 的白名單決定。

| 服務 | 指令 | 同時執行 | 更新方式 | 停止的等待時間 |
|---|---|---|---|---|
| bot | `python main.py`（image 的預設指令） | 只能 1 個 | 先停止舊的，再啟動新的 | 30 秒 |
| worker | `taskiq worker bot.worker.broker:broker bot.worker.tasks --workers 1` | 1 個以上 | 可以先啟動新的 | 30 分鐘 |
| scheduler | `taskiq scheduler bot.worker.scheduler:scheduler bot.worker.tasks` | 只能 1 個 | 先停止舊的，再啟動新的 | 預設即可 |
| migration | `alembic upgrade head` | 每次部署執行一次 | 在更新三個服務之前 | — |

- **bot 同一時間只能有一個**：如果滾動更新先啟動新版本、再停止舊版本，會有一段時間兩個 bot 同時連著 Discord，事件會被處理兩次（例如訊息被存兩次）。代價是更新時 bot 會中斷幾秒，API 與 Discord 上的按鈕暫時沒有回應。
- **scheduler 也只能有一個**：有兩個的話，定時工作會被放進 queue 兩次。
- 程式也會用 **Redis 鎖**保證 bot 和 scheduler 只有一個在執行（`ticket-bot:lock:bot`、`ticket-bot:lock:scheduler`，`bot/core/instance_lock.py`）：
  - 啟動時先取得鎖，取不到就一直等，log 會寫 `Another bot is running, waiting for it to stop`。bot 取得鎖之後才登入 Discord、啟動 API；scheduler 取得鎖之後才開始放入工作。等待期間收到 `SIGTERM` 會直接結束。
  - 持有鎖的程序每 10 秒延長一次，鎖在 30 秒後過期。正常停止時會馬上釋放（bot 在 Discord 的連線關閉之後才釋放）；當機的話，其他的最多等 30 秒。
  - 連不上 Redis 時繼續執行，每秒重試一次。鎖不見了（例如 Redis 重新啟動），而且還沒有被其他程序拿走時，直接拿回來。
  - 發現鎖被其他程序拿走時（例如程序停住太久，鎖已經過期），馬上關閉，並用非 0 的狀態碼結束。
  - 所以部署平台設錯成「先啟動新的」，也不會同時執行兩個，新的會等舊的停止。但如果平台要等新的通過健康檢查才停止舊的，新的在等鎖時還沒有啟動 API，健康檢查不會通過，部署就會卡住。所以平台還是要設定成「先停止舊的，再啟動新的」。
- **停止的等待時間**是部署平台送出 `SIGTERM` 之後，等多久才強制停止：
  - worker 收到 `SIGTERM` 後不再接新工作，等目前的工作完成才結束。等待時間要比最長的工作（上傳 1 GB 的附件）還長，所以設成跟 `idle_timeout` 一樣的 30 分鐘。沒有工作在執行時，幾秒就會停止。等更久也沒有意義，因為超過 30 分鐘沒有 ack 的工作，本來就會被其他 worker 接手。
  - 被強制停止時工作不會遺失，但要等 30 分鐘才會被接手重新執行。
  - bot 關閉時，最多等 SSE 等連線 10 秒（`timeout_graceful_shutdown`），所以 30 秒就夠了。
- **PID 1**：image 用 tini 當 PID 1，把 `SIGTERM` 轉給程式。PID 1 沒有自己處理的訊號會被忽略。bot 和 scheduler 在等鎖的時候還沒有處理 `SIGTERM`（taskiq 的 scheduler 本身也沒有處理），沒有 tini 的話，就要等到被強制停止。換掉 `ENTRYPOINT` 時要注意這一點。
- **migration**：每次部署先用同一個 image 執行一次 `alembic upgrade head`，再更新三個服務。部署期間新舊版本會同時執行一段時間（migration 時舊版本還在跑，worker 也可以先啟動新的），所以 migration 和工作的參數都要跟上一版相容：
  - 新增欄位要允許 NULL，或是有預設值。
  - 刪除欄位或改名要分兩次部署：先讓程式不再使用，下一次再刪除。
- **健康檢查**：只有 bot 有 HTTP（`GET /health`）。image 裡沒有 curl，可以用 `python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"`。三個服務共用 image，所以健康檢查不寫在 `Dockerfile` 裡。
- **網路**：image 預設 `API_HOST=0.0.0.0`。API 的 port（`API_PORT`，預設 8000）只讓自己的 reverse proxy 連進來，`FORWARDED_ALLOW_IPS` 設成 reverse proxy 的 IP（`docs/auth.md` 第 8 節）。worker、scheduler 不需要對外的 port。
- **SQLite** 只適合開發或單一主機：三個服務要掛同一個 volume 在 `/app/data`，而且容器裡的使用者（uid 10001）要能寫入。正式環境使用 MySQL、MariaDB 或 PostgreSQL。

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
- **附件與頭像**：依靠 S3 服務本身的靜態加密，應用程式層不另外加密。bot 上傳時不帶伺服器端加密的參數，因為自架的 S3 相容服務在沒有設定金鑰管理（KMS）時會直接拒絕這種請求；大部分雲端的 S3 服務預設就會加密儲存的資料，自架的話要在儲存服務那邊開啟。
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
  channel_id, webhook_id, 🔒webhook_token, opening_message_id（開單訊息，接手、轉交時更新它的 embed 與按鈕）
  opener_id, opener_added_at（null 表示開單者還沒加進頻道）, 🔒subject, 🔒content
  claimed_by_id, claimed_at
  last_message_at, inactivity_reminded_at, created_at
  closed_at, closed_by_id（null 表示系統自動關閉）, 🔒close_reason
  close_notified_at（關單私訊都處理完的時間，null 表示還沒）
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
  🔒content（最新的內容）, created_at, edited_at, deleted_at（被刪除時內容不清空，8.1）
  delivery_status（網頁訊息才有：pending | sent | failed）, attempts

ticket_message_revision（編輯前的內容，一筆是一個舊版本，8.1）
  id, message_id, 🔒content
  created_at（這一版開始的時間：送出或編輯成這一版）, replaced_at（被編輯掉的時間）

ticket_attachment
  id, ticket_id, message_id（網頁上傳時，送出訊息前是 null）, uploader_id
  source（discord | web）, discord_attachment_id（worker 用來重新取得 Discord 的下載網址）
  source_url（Discord 的下載網址，會過期；過期時用 discord_attachment_id 重新取得）, storage_key
  filename, content_type, size
  status（pending | done | rejected | failed）, attempts
  upload_id（網頁的 multipart upload）, uploaded_at（網頁上傳完成的時間，null 表示還在上傳）
  share_token_hash（分享連結 token 的 SHA-256，只有網頁上傳的附件才有）, share_revoked_at
  removed_at（編輯訊息時被移除的時間，檔案保留）
  created_at

ticket_event（時間軸、稽核紀錄；所有 SSE 事件都會寫在這裡）
  id, ticket_id, type, actor_id（null 表示系統）, data（JSON）, created_at
```

主要的索引：

- `ticket(opener_id, status)`：檢查同時開單的上限。
- `ticket(channel_id)`（唯一）：按鈕和 `on_message` 用頻道查客服單。
- `ticket(status, last_message_at)`：掃描閒置的客服單。
- `ticket(status, opener_added_at)`：找出開單者還沒加進頻道的客服單。
- `ticket(status, closed_at)`：找出私訊還沒處理完的客服單。
- `ticket_message(ticket_id, id)`、`ticket_event(ticket_id, id)`：依照順序分頁讀取、SSE 補送。
- `ticket_message(discord_message_id)`（唯一）：同一則訊息不會存兩次；編輯、刪除時用它找訊息。
- `ticket_message_revision(message_id)`：列出訊息時一起讀取編輯前的內容。

### 13.1 各資料庫的差異

SQLite、MySQL、MariaDB、PostgreSQL 都用同一套程式，測試也都要能通過（`uv run pytest --db mysql` 等，資料庫用 `compose.yaml` 的 `db` profile）。已經處理的差異：

- **交易隔離等級**：MySQL / MariaDB 的連線改用 `READ COMMITTED`，跟 PostgreSQL 的預設一樣。程式裡很多地方用「帶條件的 UPDATE，看更新了幾筆」來處理同時操作（評分、閒置提醒、附件），要依照最新 commit 的資料判斷。預設的 `REPEATABLE READ` 下，MariaDB 11.8 起（`innodb_snapshot_isolation`）更新一筆讀過之後被其他交易改過的資料，會直接失敗（錯誤 1020），而不是更新 0 筆。
- **時間**：MySQL / MariaDB 的 `DATETIME` 預設只到秒，所以指定到微秒（`fsp=6`）。
- **字元集**：連線指定 `charset=utf8mb4`；每個資料表也自己指定 `utf8mb4`（`bot/db/base.py`，migration 的 `mysql_charset`），不依賴資料庫的預設字元集。舊版 MySQL / MariaDB 的預設是 `latin1`，存不進中文和 emoji。排序規則用伺服器對 `utf8mb4` 的預設。測試用的 MySQL / MariaDB 故意把預設字元集設成 `latin1`，確認這一點。
- **migration 的 downgrade**：MySQL / MariaDB 不能刪除外鍵用到的索引，所以刪除資料表時直接刪，不要先刪它的索引。Alembic 自動產生的 downgrade 會先刪索引，要手動改掉。

## 14. API

- bot 有自己的網域，endpoint 直接放在根路徑底下。
- 除了 `GET /health`、`POST /auth/exchange`、`GET /a/{attachment_id}/{token}` 以外，所有 endpoint 都需要登入（bot 的 cookie），權限依照第 4 節判斷。
- Discord ID 在 JSON 裡一律用**字串**，因為 snowflake 超過 JavaScript 的安全整數範圍。
- 回應用 gzip 壓縮（`GZipMiddleware`，等級 6），瀏覽器會自動解壓縮。小於 500 bytes 的回應不壓縮；SSE（`text/event-stream`）也不壓縮，否則事件會被緩衝住，不能即時送到瀏覽器。
  - BREACH 攻擊要回應裡同時有祕密和攻擊者能控制的內容。這裡的 cookie 在 header 裡，不會被壓縮；回應裡的 presigned URL，也是看得到這張單的人本來就拿得到的，所以沒有實際的風險。
- 列表類的 endpoint（客服單、訊息）使用 cursor 分頁（`?before=<id>&limit=50`，`limit` 最多 100），不用 offset：訊息一直在增加，用 offset 會漏掉或重複。
  - `GET /tickets`：新的在前面，下一頁用這一頁最後一筆的 id 當作 `before`。篩選：`status`、`category_id`、`opener_id`、`claimed_by_id`、`claimed`（`false` 是還沒有人接手）、`q`（開單者的名稱，不分大小寫）。每一筆附上開單者與接手的人的快照。
  - `GET /tickets/{id}/messages`：舊的在前面。沒有指定時是最新的 `limit` 則；`before` 往前翻頁（用這一頁第一則的 id）；`after` 取得之後的訊息（收到 SSE 的 `message.created` 時；收到 `message.updated`、`message.deleted` 時用 `?after=<message_id - 1>&limit=1` 重新取得那一則）。回應的 `users` 是這些訊息的作者的快照。
    - 編輯過的訊息有 `edited_at`、被刪除的有 `deleted_at`。編輯前的每一版（`revisions`）、被刪除的訊息的內容與附件、編輯時移除的附件（`removed_at`），只有這張單的客服、管理員拿得到；其他人拿到的被刪除的訊息 `content` 是空的、沒有附件（8.1）。
- 回傳使用者資料時用快照（8.6），頭像依照 8.6 的順序決定。`GET /tickets/{id}` 回傳這張單所有人的快照，以及額外加入的成員。

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
| 附件 | `POST /tickets/{id}/attachments`（取得上傳網址）、`POST /attachments/{attachment_id}/parts`（分塊上傳每一塊的網址）、`POST /attachments/{attachment_id}/complete`、`DELETE /attachments/{attachment_id}`（移除還沒送出的附件）、`GET /attachments/{attachment_id}`（附件的資料與新的 presigned URL） | 依照權限；上傳的操作只有上傳的人，還沒送出的附件也只有上傳的人看得到；編輯時移除的附件、被刪除的訊息的附件只有客服、管理員看得到 |
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
| 訊息的編輯、刪除 | 同步 Discord 上的編輯、刪除；保留編輯前的每一版與被刪除的內容，網頁上只有客服、管理員看得到 | 處理爭議時有紀錄；開單者看到的跟 Discord 上一樣，貼錯的東西刪掉後其他成員就看不到 |
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
                      S3_ENDPOINT 留空表示 AWS S3
S3_PRESIGN_TTL        presigned URL 的有效期限（秒，預設 600）
PUBLIC_BASE_URL       bot 對外的網址（Discord 要連得到），用來組出分享連結（8.4.1）
WEB_TICKET_URL        紀錄頁面網址的格式，例：https://.../tickets/{id}
```

API 與身分驗證相關的環境變數見 `docs/auth.md`。

## 17. 需要實測的項目

- 用公開的 S3 與 bot 網址，從網頁上傳到 Discord 顯示預覽，從頭到尾測一次。目前 `302` 轉址與 `[檔名](網址)` 的寫法分開測過都會顯示預覽，還沒有兩者一起測。
- 公開的 S3 服務（R2、AWS S3）是否跟 Silo 一樣，拒絕大小跟簽章不同的上傳（8.2）。不會的話，`verify_attachment` 仍然會檢查大小。
