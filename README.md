# tgdl — Telegram media downloader

> 繁體中文說明在下方 / Traditional Chinese below.

Downloads media from Telegram chats you have joined (user-account login via
MTProto/Telethon), organizes it by chat / category / date, and is managed from
a local web UI. Single foreground process, no systemd or Docker required.

**Status:** Phase a (core) is done: login, one-shot download of a chat,
dedup DB, path & category logic. Monitors, workers and the web UI come next.

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

### 單次下載（Phase a）

```bash
.venv/bin/tgdl --lang zh-TW fetch @channel_name --limit 50
```
