-- tgdl schema v2. All timestamps are UTC ISO-8601 strings.

CREATE TABLE IF NOT EXISTS settings (
  key         TEXT PRIMARY KEY,
  value       TEXT NOT NULL,           -- JSON
  updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS admin (
  id            INTEGER PRIMARY KEY CHECK (id = 1),
  username      TEXT,                  -- NULL until set with `tgdl set-password`
  password_hash TEXT NOT NULL,         -- argon2id
  updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS web_sessions (
  token_hash  TEXT PRIMARY KEY,        -- sha256 of the cookie token
  csrf_token  TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  expires_at  TEXT NOT NULL,
  last_seen   TEXT NOT NULL,
  client_ip   TEXT
);

CREATE TABLE IF NOT EXISTS login_failures (
  scope         TEXT PRIMARY KEY,      -- 'ip:<addr>' or 'global'
  fail_count    INTEGER NOT NULL,
  window_start  TEXT NOT NULL,
  locked_until  TEXT
);

CREATE TABLE IF NOT EXISTS monitors (
  chat_id         INTEGER PRIMARY KEY, -- marked peer id (-100... for channels)
  chat_title      TEXT NOT NULL,
  chat_type       TEXT NOT NULL,       -- channel / group / user
  enabled         INTEGER NOT NULL DEFAULT 1,
  media_types     TEXT,                -- JSON list, NULL = global default
  download_dir    TEXT,                -- NULL = global default
  filename_prefix TEXT,
  min_size        INTEGER,
  max_size        INTEGER,
  keyword         TEXT,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id         INTEGER NOT NULL,
  chat_title      TEXT,
  message_id      INTEGER NOT NULL,
  grouped_id      INTEGER,
  media_unique_id TEXT NOT NULL,
  media_kind      TEXT NOT NULL,
  file_name       TEXT,
  file_size       INTEGER,
  message_date    TEXT NOT NULL,
  source          TEXT NOT NULL,       -- live / backfill / retry / redownload / manual
  status          TEXT NOT NULL,       -- pending / running / done / failed / skipped / cancelled
  attempts        INTEGER NOT NULL DEFAULT 0,
  priority        INTEGER NOT NULL DEFAULT 0, -- lower first: live=0, backfill=10
  not_before      TEXT,                -- retry backoff: not claimable before this
  part_path       TEXT,                -- .part file being written (crash cleanup)
  last_error      TEXT,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_jobs_active_msg
  ON jobs(chat_id, message_id) WHERE status IN ('pending', 'running');
CREATE UNIQUE INDEX IF NOT EXISTS ux_jobs_active_media
  ON jobs(media_unique_id) WHERE status IN ('pending', 'running');
CREATE INDEX IF NOT EXISTS ix_jobs_claim ON jobs(status, priority, id);
CREATE INDEX IF NOT EXISTS ix_jobs_chat ON jobs(chat_id);

CREATE TABLE IF NOT EXISTS downloads (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id         INTEGER NOT NULL,
  message_id      INTEGER NOT NULL,
  media_unique_id TEXT NOT NULL,
  grouped_id      INTEGER,
  chat_title      TEXT,
  category        TEXT NOT NULL,
  media_kind      TEXT NOT NULL,
  file_name       TEXT NOT NULL,
  file_size       INTEGER NOT NULL,
  saved_path      TEXT NOT NULL,
  message_date    TEXT NOT NULL,
  downloaded_at   TEXT NOT NULL,
  UNIQUE (chat_id, message_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_downloads_media ON downloads(media_unique_id);
CREATE INDEX IF NOT EXISTS ix_downloads_date ON downloads(downloaded_at);
CREATE INDEX IF NOT EXISTS ix_downloads_chat ON downloads(chat_id);

CREATE TABLE IF NOT EXISTS backfill_tasks (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id     INTEGER NOT NULL,
  date_from   TEXT NOT NULL,
  date_to     TEXT NOT NULL,
  status      TEXT NOT NULL,           -- running / done / failed / cancelled
  scanned     INTEGER NOT NULL DEFAULT 0,
  enqueued    INTEGER NOT NULL DEFAULT 0,
  error       TEXT,
  created_at  TEXT NOT NULL,
  finished_at TEXT
);
