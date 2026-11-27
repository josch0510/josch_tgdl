# tgdl — Telegram media downloader

> 繁體中文說明在下方 / Traditional Chinese below.

Downloads media from Telegram groups and channels you have joined (user-account
login via MTProto/Telethon), organizes it by chat / category / date, and is
managed from a local web UI. One foreground process — no systemd, no Docker.

- Monitor many chats at once; enable/disable each; per-chat media types,
  directory, filename prefix, size and filename-keyword filters
- Backfill a chat's history for a date range
- Concurrent downloads (1–10, changeable at runtime), live progress in the browser
- FloodWait handling with a countdown in the UI; automatic retries
- Dedup by message and by file (a file forwarded to several chats is saved once)
- Download history with search/filter, retry of failed downloads, re-download
  of files that were deleted from disk
- Log viewer with level filter and live mode; separate audit log
- UI and CLI in English and Traditional Chinese

## Requirements

Debian 13 (Python 3.13) or any Linux with Python ≥ 3.11. A Telegram account and
an API ID/hash from <https://my.telegram.org> → *API development tools*.

## Setup

```bash
sudo apt install python3-venv git
git clone https://github.com/josch0510/josch_tgdl.git tgdl && cd tgdl
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock -e .
cp .env.example .env && chmod 600 .env
$EDITOR .env
```

`.env` settings:

| Key | Meaning |
|---|---|
| `API_ID`, `API_HASH` | Telegram API credentials (never shown in the UI or logs) |
| `ALLOWED_BASE_DIRS` | Directories files may be written to, separated by `:`. The web UI can only pick directories **inside** these. |
| `WEB_HOST`, `WEB_PORT` | Web UI bind address, default `127.0.0.1:8080` |
| `ALLOWED_HOSTS` | Extra host *names* for the UI, comma separated (localhost, IP addresses and this machine's own host name always work) |
| `COOKIE_SECURE` | `true` only if the UI is served over HTTPS |

## First run

```bash
.venv/bin/tgdl login          # phone, login code, 2FA password (if set)
.venv/bin/tgdl set-password   # web admin password, min. 12 characters
.venv/bin/tgdl serve          # foreground; Ctrl+C to stop
```

Add `--lang zh-TW` (or set `LANG=zh_TW.UTF-8`) for Chinese CLI messages.
`tgdl serve` runs in the foreground; to keep it running after you log out of
SSH, start it inside `tmux` or `screen`.

### Accessing the web UI (SSH tunnel)

The UI only listens on 127.0.0.1. From your own computer:

```bash
ssh -N -L 8080:127.0.0.1:8080 youruser@your-debian-host
```

then open <http://localhost:8080>.

## Using the web UI

| Page | What you can do |
|---|---|
| Dashboard | Active downloads (progress, speed, ETA), queue, failures, today's files/size, per-chat stats, FloodWait countdown |
| Monitors | Search joined chats and add them; enable/disable/remove; per-chat settings; backfill a date range; backfill task list |
| History | Search by chat, category, date, filename; *Failed* tab with retry (one or all); re-download files missing on disk |
| Logs | Tail of `tgdl.log` or `audit.log`, minimum level, text filter, live updates |
| Settings | Default directory, concurrency, media types, path template, prefix, timezone, category mapping, default language |

All settings are stored in SQLite and take effect immediately.

## File organization

Default path template:

```
{base_dir}/{chat_name}/{category}/{YYYY}/{YYYY-MM-DD}/{prefix}{msg_id}_{filename}
```

Variables: `{base_dir} {chat_name} {chat_id} {category} {YYYY} {MM} {DD}
{YYYY-MM-DD} {msg_id} {grouped_id} {prefix} {filename}`. Dates are the message
date in the configured timezone (default `Asia/Taipei`); generated names of
photos without a filename use the same timezone. The prefix may use
`{chat_name} {date} {msg_id} {category}`.

Categories: Photos, Videos, Audio, Voice, Documents, Archives, Others —
decided by Telegram media type first, then file extension, then MIME type
(all editable in Settings). Split archive parts — `.7z.001`, `.zip.002`,
`.z01`, `.r00` — are classified by their archive format (7z, zip, rar), so they
land in Archives without extra configuration.

Every path component is sanitized (no `/`, `..`, control characters; max 255
bytes in UTF-8 without breaking CJK characters), and the final path is checked
to be inside `ALLOWED_BASE_DIRS` after resolving symlinks.

Downloads are written to `<name>.part`, size-checked, then renamed. Existing
files are never overwritten (`_1`, `_2`, … is appended). A history record is
stored only after the rename.

## Behaviour details

- **Queue** is stored in SQLite and survives restarts. Live messages are
  downloaded before backfill jobs. The same message or the same file is never
  queued twice.
- **Dedup**: skipped if the message *or* the same Telegram file (photo/document
  id) was already downloaded, in any chat.
- **FloodWait**: all downloads pause for the time Telegram requests (chat-list
  and history scans pause separately); downloads already running finish first.
- **Errors**: network/server errors retry after 5 s, 10 s, 20 s; then the job is
  *failed* (retry it from History). A deleted message fails immediately.
- **Albums**: each item is its own job; missing album items are fetched
  automatically.
- **Ctrl+C**: stop accepting jobs, interrupt running downloads, delete their
  `.part` files and put them back in the queue. Press twice to force quit.
  After a crash, leftover `.part` files are removed on the next start.
- **Telegram disconnects**: reconnects automatically; messages posted while
  offline are caught up.

## Web security

- Binds to 127.0.0.1; host-name allowlist against DNS rebinding (IP addresses
  localhost and this machine's own host name always accepted; other names via
  `ALLOWED_HOSTS`)
- One admin account, argon2id password hash, verified off the event loop
- Server-side sessions; cookie `HttpOnly; SameSite=Strict`; 2 h idle / 12 h
  absolute expiry; all sessions revoked when the password changes
- CSRF token + same-origin check on every state-changing request; the login
  form has its own CSRF token
- 5 failed logins from one IP (20 in total) within 15 min → locked for 15 min
- Strict Content-Security-Policy (no inline scripts or styles), no framing,
  `no-store` caching; htmx is served locally, no CDN
- API hash, phone number and session data are never shown in the UI and are
  redacted from logs
- `.env`, the session file, the database and the logs are mode 600
  (`data/` and `logs/` 700)

## CLI reference

```
tgdl [--lang en|zh-TW] [--home DIR] COMMAND
  login                                    interactive Telegram login
  set-password                             set the web admin password
  serve                                    web UI + monitors + workers (foreground)
  monitor list|add|remove|enable|disable [CHAT]
  fetch CHAT [--limit N]                   one-shot download of the latest N messages
```

`--home` (or `TGDL_HOME`) selects the directory holding `.env`, `data/` and
`logs/` (default: current directory). argparse's own headings (`usage:`,
`options:`) stay in English.

## Files

| Path | Content | Mode |
|---|---|---|
| `.env` | API credentials, allowed dirs, bind address | 600 |
| `data/tgdl.session` | Telegram login session — treat like a password | 600 |
| `data/tgdl.db` | settings, monitors, queue, history, web sessions | 600 |
| `logs/tgdl.log` | application log, always English, 10 MB × 5 | 600 |
| `logs/audit.log` | logins, settings and monitor changes, retries | 600 |

Log format: `timestamp | level | chat=… | msg_id=… | action=… | reason=… | details`.

## Troubleshooting

| Problem | Fix |
|---|---|
| `Not logged in to Telegram` | Run `tgdl login` again (e.g. after the session was terminated in Telegram) |
| Web UI says no password is set | `tgdl set-password` |
| Locked out after wrong passwords | Wait 15 minutes, or stop tgdl and run `sqlite3 data/tgdl.db "DELETE FROM login_failures"` |
| Browser shows `Host not allowed` | You opened the UI by a host *name* other than this machine's own name. Use `http://localhost:8080` via the SSH tunnel or the server's IP, or add the name to `ALLOWED_HOSTS` in `.env` and restart |
| `Cannot start the web UI … Address already in use` | Another program uses the port: change `WEB_PORT` |
| "Directory is outside the allowed base directories" | Add the parent directory to `ALLOWED_BASE_DIRS` in `.env` and restart |
| Long FloodWait | Normal Telegram rate limiting; lower the concurrency in Settings |

## Development

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

Layout of `src/tgdl/`: `core/` (sanitizing, paths, categories, filters),
`storage/` (SQLite), `telegram/` (client, monitors, backfill), `downloader/`
(pipeline, queue, workers, FloodWait), `web/` (routes, templates, static),
`locales/` (en, zh-TW).

---

## 繁體中文說明

用您自己的 Telegram 帳號（MTProto / Telethon）下載已加入群組與頻道中的媒體，依對話、分類、日期自動整理，並透過本機網頁介面管理。只需要一個前景程式，不需要 systemd 或 Docker。

### 功能

- 同時監控多個對話，可個別啟用或停用，並可個別設定媒體類型、目錄、檔名前綴、檔案大小與檔名關鍵字過濾
- 依日期範圍回補歷史訊息
- 同時下載數可設 1 到 10，執行中即可調整；網頁上即時顯示進度
- 遇到 FloodWait 時，網頁會顯示倒數；錯誤會自動重試
- 依訊息與檔案去重，同一個檔案被轉發到多個對話時只會存一份
- 下載紀錄可搜尋、篩選，可重試失敗的下載，也可重新下載磁碟上已被刪除的檔案
- 日誌檢視可依等級篩選並即時更新；另有獨立的稽核日誌
- 網頁介面與 CLI 都支援繁體中文和英文

### 安裝（Debian 13）

```bash
sudo apt install python3-venv git
git clone https://github.com/josch0510/josch_tgdl.git tgdl && cd tgdl
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock -e .
cp .env.example .env && chmod 600 .env
$EDITOR .env
```

`.env` 的設定項目：

| 項目 | 說明 |
|---|---|
| `API_ID`、`API_HASH` | 到 <https://my.telegram.org> 的 *API development tools* 申請；不會出現在網頁或日誌中 |
| `ALLOWED_BASE_DIRS` | 允許寫入檔案的根目錄，多個目錄用 `:` 分隔。網頁上只能選擇這些目錄**底下**的位置 |
| `WEB_HOST`、`WEB_PORT` | 網頁介面監聽位址，預設 `127.0.0.1:8080` |
| `ALLOWED_HOSTS` | 額外允許的主機「名稱」，以逗號分隔（localhost、IP 位址與本機主機名稱一律可用） |
| `COOKIE_SECURE` | 只有透過 HTTPS 提供網頁時才設為 `true` |

### 第一次執行

```bash
.venv/bin/tgdl --lang zh-TW login          # 電話、登入驗證碼、兩步驟驗證密碼
.venv/bin/tgdl --lang zh-TW set-password   # 網頁管理員密碼（至少 12 字元）
.venv/bin/tgdl --lang zh-TW serve          # 前景執行，Ctrl+C 停止
```

若要在登出 SSH 後讓程式繼續執行，請在 `tmux` 或 `screen` 中啟動。

### 透過 SSH tunnel 連線

網頁介面只監聽 127.0.0.1。請在您自己的電腦執行：

```bash
ssh -N -L 8080:127.0.0.1:8080 您的帳號@debian主機
```

然後用瀏覽器開啟 <http://localhost:8080>。

### 網頁功能

| 頁面 | 功能 |
|---|---|
| 儀表板 | 進行中的下載（進度、速度、剩餘時間）、佇列、失敗數、今日檔案數與下載量、各對話統計、FloodWait 倒數 |
| 對話監控 | 搜尋已加入的對話並加入監控；啟用、停用、移除；每個對話的個別設定；依日期範圍回補歷史訊息 |
| 下載紀錄 | 依對話、分類、日期、檔名搜尋；「失敗」分頁可重試單筆或全部；重新下載磁碟上遺失的檔案 |
| 日誌 | 檢視 `tgdl.log` 或 `audit.log` 的最新內容，可依等級和文字篩選，並可即時更新 |
| 設定 | 預設目錄、同時下載數、媒體類型、路徑模板、前綴、時區、分類對應、預設語言 |

所有設定都存在 SQLite 中，修改後立即生效，不需要重新啟動。

### 檔案整理

預設路徑模板：

```
{base_dir}/{chat_name}/{category}/{YYYY}/{YYYY-MM-DD}/{prefix}{msg_id}_{filename}
```

- 日期是訊息時間換算成設定時區後的日期（預設 `Asia/Taipei`）；沒有檔名的相片，自動產生的檔名也使用同一個時區
- 分類判斷順序：先看 Telegram 媒體類型，再看副檔名，最後看 MIME 類型，都不符合則歸為 Others；對應規則可在「設定」頁修改
- 壓縮分割檔（`.7z.001`、`.zip.002`、`.z01`、`.r00`）會依原本的壓縮格式（7z、zip、rar）分類，自動歸入 Archives，不需要另外設定副檔名
- 每一段路徑都會經過清理：移除 `/`、`..` 與控制字元，並以 UTF-8 bytes 截斷到 255，不會切壞中文字元
- 最終路徑會在解析 symlink 之後確認位於 `ALLOWED_BASE_DIRS` 之下
- 下載時先寫入 `.part`，確認大小正確後才改名，之後才寫入下載紀錄；已存在的檔案不會被覆蓋，會自動加上 `_1`、`_2`

### 運作細節

- 佇列存在 SQLite，重新啟動後會繼續；即時訊息優先於歷史回補；同一則訊息或同一個檔案不會重複加入佇列
- 遇到 FloodWait 時，進行中的下載會先完成，之後暫停到 Telegram 要求的時間結束
- 網路或伺服器錯誤會在 5、10、20 秒後重試，三次都失敗就標記為失敗，可到「下載紀錄」重試
- 相簿的每個項目都是獨立的工作，缺漏的項目會自動補抓
- Ctrl+C 會停止接收新工作、中斷進行中的下載、刪除 `.part` 並放回佇列；連按兩次可強制結束
- 異常中斷後，下次啟動時會清掉殘留的 `.part`
- 與 Telegram 斷線時會自動重連，並補收離線期間的訊息

### 疑難排解

| 狀況 | 解決方式 |
|---|---|
| 顯示「尚未登入 Telegram」 | 重新執行 `tgdl login` |
| 網頁顯示尚未設定密碼 | 執行 `tgdl set-password` |
| 密碼輸錯太多次被鎖定 | 等 15 分鐘；或停止 tgdl 後執行 `sqlite3 data/tgdl.db "DELETE FROM login_failures"` |
| 瀏覽器顯示 `Host not allowed` | 您用的主機名稱不是本機的主機名稱。請透過 SSH tunnel 使用 `http://localhost:8080`、改用伺服器 IP，或把該名稱加入 `.env` 的 `ALLOWED_HOSTS` 後重新啟動 |
| 「無法啟動網頁介面…Address already in use」 | 埠號被其他程式占用，請修改 `WEB_PORT` |
| 「目錄不在允許的下載根目錄之下」 | 在 `.env` 的 `ALLOWED_BASE_DIRS` 加入該目錄的上層目錄，然後重新啟動 |
| FloodWait 時間很長 | 這是 Telegram 正常的速率限制，可在「設定」頁調低同時下載數 |

注意：`tgdl --help` 中由 argparse 產生的標題（例如 `usage:`、`options:`）固定為英文，其餘說明文字會依語言顯示。
