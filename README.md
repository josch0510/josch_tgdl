# tgdl — Telegram media downloader

> 繁體中文說明在下方 / Traditional Chinese below.

Downloads media from Telegram chats you have joined (user-account login via
MTProto/Telethon), organizes it by chat / category / date, and is managed from
a local web UI. Single foreground process, no systemd or Docker required.

**Status:** Phases a–b done: login, dedup DB, path/category logic, persistent
queue, concurrent workers, multi-chat monitors, backfill, FloodWait handling.
The web UI (phase c) comes next; until then monitors are managed via CLI.

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

## Run (phase b: no web UI yet)

```bash
.venv/bin/tgdl monitor add @channel_name    # or a numeric id / t.me link
.venv/bin/tgdl monitor list
.venv/bin/tgdl serve                        # foreground; Ctrl+C to stop
```

- New media in enabled monitors is queued and downloaded by a worker pool
  (concurrency: setting `concurrency`, default 3, max 10).
- The queue lives in SQLite: jobs survive a restart. Live messages are
  downloaded before backfill jobs.
- FloodWait: all downloads pause for the time Telegram asks (logged as
  `action=flood_wait`, shown in the web UI from phase c).
- Network/server errors retry after 5s, 10s, 20s, then the job is marked failed.
- Ctrl+C: stops accepting jobs, interrupts running downloads, deletes their
  `.part` files and puts them back in the queue. Press Ctrl+C twice to force quit.

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

### 執行（Phase b，尚無網頁介面）

```bash
.venv/bin/tgdl --lang zh-TW monitor add @channel_name
.venv/bin/tgdl --lang zh-TW serve     # 前景執行，Ctrl+C 停止
```

- 佇列存在 SQLite，重新啟動後未完成的工作會繼續；即時訊息優先於歷史回補。
- 遇到 FloodWait 會暫停所有下載，等候 Telegram 要求的秒數。
- 網路或伺服器錯誤會在 5、10、20 秒後重試，三次都失敗就標記為失敗。
- Ctrl+C 會停止收新工作、中斷進行中的下載、刪除 `.part` 並放回佇列；連按兩次強制結束。

### 單次下載

```bash
.venv/bin/tgdl --lang zh-TW fetch @channel_name --limit 50
```
