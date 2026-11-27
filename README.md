# tgdl — Telegram media downloader

> 繁體中文說明在下方 / Traditional Chinese below.

Downloads media from Telegram chats you have joined (user-account login via
MTProto/Telethon), organizes it by chat / category / date, and is managed from
a local web UI. Single foreground process, no systemd or Docker required.

**Status:** Phases a–c done: core, queue/workers/monitors, and the web UI
(login, dashboard, monitors, settings). Download history and log viewer (phase d)
come next.

## Setup (Debian 13)

```bash
sudo apt install python3-venv
git clone <this repo> tgdl && cd tgdl
python3 -m venv .venv
.venv/bin/pip install -e .            # add [dev] for tests: -e '.[dev]'
cp .env.example .env && chmod 600 .env
$EDITOR .env                          # API_ID, API_HASH, ALLOWED_BASE_DIRS
```

Get `API_ID` / `API_HASH` at <https://my.telegram.org> → *API development tools*.

`ALLOWED_BASE_DIRS` (separated by `:`) is the only place that decides where
files may be written. The web UI can only choose directories inside it.

## First login

```bash
.venv/bin/tgdl login          # phone, code, 2FA password; --lang zh-TW for Chinese
.venv/bin/tgdl set-password   # web admin password (min 12 chars, argon2id)
```

The session is stored in `data/tgdl.session` (mode 600). Treat it like a
password: anyone with this file can use your Telegram account.

## One-shot download (phase a)

```bash
.venv/bin/tgdl fetch @channel_name --limit 50
.venv/bin/tgdl fetch -1001234567890 --limit 50
```

## Run

```bash
.venv/bin/tgdl serve          # foreground; Ctrl+C to stop
```

The web UI listens on `127.0.0.1:8080` (change `WEB_HOST` / `WEB_PORT` in `.env`).
From your own computer, open an SSH tunnel and browse to <http://localhost:8080>:

```bash
ssh -N -L 8080:127.0.0.1:8080 youruser@your-debian-host
```

- **Monitors**: search your joined groups/channels, add, enable/disable, remove,
  per-chat settings (media types, directory, prefix, size/keyword filters),
  and backfill a date range.
- **Dashboard**: active downloads with progress/speed/ETA (live via SSE), queue,
  today's count/size, per-chat stats, and a FloodWait countdown banner.
- **Settings**: default directory, concurrency (applied immediately), path
  template, prefix, timezone, category mapping, default language.
- Monitors can also be managed from the terminal: `tgdl monitor list|add|remove|enable|disable`.

Downloads: the queue is stored in SQLite and survives restarts; live messages
go before backfill. FloodWait pauses all downloads for the time Telegram asks.
Network/server errors retry after 5s, 10s, 20s, then the job is marked failed.
Ctrl+C stops accepting jobs, interrupts running downloads, deletes their `.part`
files and puts them back in the queue (press twice to force quit).

### Web security

- Binds to 127.0.0.1; only `localhost`/`127.0.0.1` (plus `ALLOWED_HOSTS`) are
  accepted as Host header (DNS-rebinding protection).
- Single admin, argon2id password; server-side sessions (HttpOnly,
  SameSite=Strict cookie, 2h idle / 12h absolute expiry).
- CSRF token + same-origin check on every POST; strict Content-Security-Policy.
- 5 failed logins from one IP (or 20 overall) within 15 minutes → locked for 15 minutes.
- Download directories must resolve (symlinks included) inside `ALLOWED_BASE_DIRS`,
  which can only be changed in `.env`.
- All logins, settings and monitor changes are written to `logs/audit.log`.

## Files

| Path | Content |
|---|---|
| `.env` | API credentials, allowed dirs, bind address (600) |
| `data/tgdl.session` | Telegram session (600) |
| `data/tgdl.db` | settings, monitors, jobs, download history (600) |
| `logs/tgdl.log` | application log, English, 10MB × 5 |
| `logs/audit.log` | web/admin actions |

## Tests

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

---

## 繁體中文

### 安裝（Debian 13）

```bash
sudo apt install python3-venv
git clone <此 repo> tgdl && cd tgdl
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env && chmod 600 .env
$EDITOR .env      # 填入 API_ID、API_HASH、ALLOWED_BASE_DIRS
```

`API_ID` / `API_HASH` 請到 <https://my.telegram.org> 的 *API development tools* 申請。
`ALLOWED_BASE_DIRS` 是唯一決定檔案可以寫到哪裡的設定，網頁上只能選擇它底下的目錄。

### 首次登入

```bash
.venv/bin/tgdl --lang zh-TW login          # 電話、驗證碼、兩步驟驗證密碼
.venv/bin/tgdl --lang zh-TW set-password   # 網頁管理員密碼（至少 12 字元）
```

`data/tgdl.session` 等同您的 Telegram 帳號登入憑證，請妥善保管。

### 執行

```bash
.venv/bin/tgdl --lang zh-TW serve     # 前景執行，Ctrl+C 停止
```

網頁介面預設只監聽 `127.0.0.1:8080`。請從自己的電腦建立 SSH tunnel，再用瀏覽器開啟 <http://localhost:8080>：

```bash
ssh -N -L 8080:127.0.0.1:8080 youruser@your-debian-host
```

- **對話監控**：搜尋已加入的群組或頻道並加入監控，可啟用、停用、移除，可設定每個對話的媒體類型、目錄、前綴、大小與關鍵字過濾，也可依日期範圍回補歷史訊息。
- **儀表板**：即時顯示下載進度、速度、剩餘時間，以及佇列、今日統計、各對話統計和 FloodWait 倒數。
- **設定**：預設目錄、同時下載數（立即生效）、路徑模板、前綴、時區、分類對應、預設語言。
- 佇列存在 SQLite，重新啟動後會繼續；遇到 FloodWait 會暫停所有下載；錯誤會重試三次；Ctrl+C 會清理 `.part` 後再結束。

### 單次下載

```bash
.venv/bin/tgdl --lang zh-TW fetch @channel_name --limit 50
```
