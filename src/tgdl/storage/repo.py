"""Data access layer."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .db import Database, iso, parse_iso

ACTIVE_STATUSES = ("pending", "running")


class DuplicateDownload(Exception):
    """A download record for the same message or media already exists."""


@dataclass(frozen=True)
class NewJob:
    chat_id: int
    chat_title: str
    message_id: int
    grouped_id: int | None
    media_unique_id: str
    media_kind: str
    file_name: str | None
    file_size: int | None
    message_date: datetime
    source: str = "live"
    priority: int = 0


# ---------------------------------------------------------------- history
def _like(text: str) -> str:
    """LIKE pattern matching ``text`` literally (escape char: backslash)."""
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


@dataclass(frozen=True)
class HistoryFilter:
    chat_id: int | None = None
    category: str | None = None
    query: str = ""                 # filename substring
    since: datetime | None = None   # inclusive, UTC
    until: datetime | None = None   # exclusive, UTC


def _where(f: HistoryFilter, date_col: str, *, category: bool) -> tuple[str, list[Any]]:
    clauses, params = [], []
    if f.chat_id is not None:
        clauses.append("chat_id = ?")
        params.append(f.chat_id)
    if category and f.category:
        clauses.append("category = ?")
        params.append(f.category)
    if f.query:
        clauses.append("file_name LIKE ? ESCAPE '\\'")
        params.append(_like(f.query))
    if f.since is not None:
        clauses.append(f"{date_col} >= ?")
        params.append(iso(f.since))
    if f.until is not None:
        clauses.append(f"{date_col} < ?")
        params.append(iso(f.until))
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


class Repository:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------ settings
    async def load_settings(self) -> dict[str, Any]:
        rows = await self.db.fetchall("SELECT key, value FROM settings")
        return {r["key"]: json.loads(r["value"]) for r in rows}

    async def save_settings(self, values: dict[str, Any]) -> None:
        now = iso()
        async with self.db.transaction() as conn:
            for key, value in values.items():
                await conn.execute(
                    "INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                    (key, json.dumps(value, ensure_ascii=False), now),
                )

    # --------------------------------------------------------------- admin
    async def get_password_hash(self) -> str | None:
        row = await self.db.fetchone("SELECT password_hash FROM admin WHERE id = 1")
        return row["password_hash"] if row else None

    async def set_password_hash(self, password_hash: str, *, revoke_sessions: bool = True) -> None:
        async with self.db.transaction() as conn:
            await conn.execute(
                "INSERT INTO admin(id, password_hash, updated_at) VALUES (1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET password_hash=excluded.password_hash, "
                "updated_at=excluded.updated_at",
                (password_hash, iso()),
            )
            # Changing the password invalidates every web session.
            if revoke_sessions:
                await conn.execute("DELETE FROM web_sessions")

    # -------------------------------------------------------- web sessions
    async def create_session(self, token_hash: str, csrf_token: str, expires_at: datetime,
                             client_ip: str | None) -> None:
        now = iso()
        await self.db.execute(
            "INSERT INTO web_sessions(token_hash, csrf_token, created_at, expires_at, last_seen, "
            "client_ip) VALUES (?, ?, ?, ?, ?, ?)",
            (token_hash, csrf_token, now, iso(expires_at), now, client_ip))

    async def get_session(self, token_hash: str) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM web_sessions WHERE token_hash = ?", (token_hash,))
        return dict(row) if row else None

    async def touch_session(self, token_hash: str) -> None:
        await self.db.execute("UPDATE web_sessions SET last_seen = ? WHERE token_hash = ?",
                              (iso(), token_hash))

    async def delete_session(self, token_hash: str) -> None:
        await self.db.execute("DELETE FROM web_sessions WHERE token_hash = ?", (token_hash,))

    async def purge_sessions(self, idle_before: datetime) -> None:
        await self.db.execute("DELETE FROM web_sessions WHERE expires_at <= ? OR last_seen <= ?",
                              (iso(), iso(idle_before)))

    # ------------------------------------------------------ login failures
    async def get_login_failure(self, scope: str) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM login_failures WHERE scope = ?", (scope,))
        return dict(row) if row else None

    async def save_login_failure(self, scope: str, fail_count: int, window_start: datetime,
                                 locked_until: datetime | None) -> None:
        await self.db.execute(
            "INSERT INTO login_failures(scope, fail_count, window_start, locked_until) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(scope) DO UPDATE SET fail_count = excluded.fail_count, "
            "window_start = excluded.window_start, locked_until = excluded.locked_until",
            (scope, fail_count, iso(window_start), iso(locked_until) if locked_until else None))

    async def clear_login_failure(self, scope: str) -> None:
        await self.db.execute("DELETE FROM login_failures WHERE scope = ?", (scope,))

    # ------------------------------------------------------------ monitors
    @staticmethod
    def _monitor(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        d = dict(row)
        d["enabled"] = bool(d["enabled"])
        d["media_types"] = json.loads(d["media_types"]) if d["media_types"] else None
        return d

    async def get_monitor(self, chat_id: int) -> dict[str, Any] | None:
        return self._monitor(await self.db.fetchone("SELECT * FROM monitors WHERE chat_id = ?", (chat_id,)))

    async def list_monitors(self) -> list[dict[str, Any]]:
        rows = await self.db.fetchall("SELECT * FROM monitors ORDER BY chat_title COLLATE NOCASE")
        return [m for r in rows if (m := self._monitor(r)) is not None]

    async def add_monitor(self, chat_id: int, chat_title: str, chat_type: str) -> None:
        now = iso()
        await self.db.execute(
            "INSERT INTO monitors(chat_id, chat_title, chat_type, enabled, created_at, updated_at) "
            "VALUES (?, ?, ?, 1, ?, ?) ON CONFLICT(chat_id) DO UPDATE SET "
            "chat_title=excluded.chat_title, chat_type=excluded.chat_type, updated_at=excluded.updated_at",
            (chat_id, chat_title, chat_type, now, now),
        )

    async def update_monitor(self, chat_id: int, **fields: Any) -> None:
        allowed = {"chat_title", "enabled", "media_types", "download_dir", "filename_prefix",
                   "min_size", "max_size", "keyword"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"Unknown monitor fields: {bad}")
        if not fields:
            return
        values = dict(fields)
        if "media_types" in values and values["media_types"] is not None:
            values["media_types"] = json.dumps(sorted(values["media_types"]))
        if "enabled" in values:
            values["enabled"] = 1 if values["enabled"] else 0
        cols = ", ".join(f"{k} = :{k}" for k in values)
        values.update(chat_id=chat_id, updated_at=iso())
        await self.db.execute(
            f"UPDATE monitors SET {cols}, updated_at = :updated_at WHERE chat_id = :chat_id", values)

    async def delete_monitor(self, chat_id: int) -> None:
        await self.db.execute("DELETE FROM monitors WHERE chat_id = ?", (chat_id,))

    # ------------------------------------------------------- dedup & jobs
    async def find_duplicate(self, chat_id: int, message_id: int, media_unique_id: str,
                             *, include_queue: bool = True) -> str | None:
        """Return why this media must be skipped, or None.

        Reasons: downloaded_message, downloaded_media, queued_message, queued_media.
        """
        row = await self.db.fetchone(
            "SELECT chat_id, message_id, media_unique_id FROM downloads "
            "WHERE (chat_id = ? AND message_id = ?) OR media_unique_id = ? LIMIT 1",
            (chat_id, message_id, media_unique_id),
        )
        if row is not None:
            if row["chat_id"] == chat_id and row["message_id"] == message_id:
                return "downloaded_message"
            return "downloaded_media"
        if not include_queue:
            return None
        row = await self.db.fetchone(
            "SELECT chat_id, message_id FROM jobs WHERE status IN ('pending','running') "
            "AND ((chat_id = ? AND message_id = ?) OR media_unique_id = ?) LIMIT 1",
            (chat_id, message_id, media_unique_id),
        )
        if row is not None:
            if row["chat_id"] == chat_id and row["message_id"] == message_id:
                return "queued_message"
            return "queued_media"
        return None

    async def enqueue(self, job: NewJob) -> tuple[int | None, str | None]:
        """Insert a pending job unless it duplicates a download or an active job.

        Returns (job_id, None) on success or (None, reason) when skipped.
        The partial unique indexes make this race-free.
        """
        reason = await self.find_duplicate(job.chat_id, job.message_id, job.media_unique_id)
        if reason:
            return None, reason
        now = iso()
        try:
            job_id = await self.db.execute(
                "INSERT INTO jobs(chat_id, chat_title, message_id, grouped_id, media_unique_id, "
                "media_kind, file_name, file_size, message_date, source, status, attempts, priority, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?, 'pending', 0, ?, ?, ?)",
                (job.chat_id, job.chat_title, job.message_id, job.grouped_id, job.media_unique_id,
                 job.media_kind, job.file_name, job.file_size, iso(job.message_date), job.source,
                 job.priority, now, now),
            )
        except sqlite3.IntegrityError:
            reason = await self.find_duplicate(job.chat_id, job.message_id, job.media_unique_id)
            return None, reason or "queued_message"
        return job_id, None

    async def get_job(self, job_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,))
        return dict(row) if row else None

    async def set_job_status(self, job_id: int, status: str, *, error: str | None = None,
                             count_attempt: bool = False) -> None:
        await self.db.execute(
            "UPDATE jobs SET status = ?, last_error = ?, attempts = attempts + ?, updated_at = ? "
            "WHERE id = ?",
            (status, error, 1 if count_attempt else 0, iso(), job_id),
        )

    async def reset_running_jobs(self, *, keep_parts: bool = False) -> list[str]:
        """After a crash/restart, running jobs go back to pending (the
        interrupted attempt is not counted).

        With ``keep_parts`` (resume enabled) the .part paths stay attached
        to their jobs and nothing is returned; otherwise the paths are
        detached and returned so the caller can delete the files."""
        async with self.db.transaction() as conn:
            parts: list[str] = []
            if not keep_parts:
                rows = await conn.execute_fetchall(
                    "SELECT part_path FROM jobs WHERE status = 'running' AND part_path IS NOT NULL")
                parts = [r[0] for r in rows]
            await conn.execute(
                "UPDATE jobs SET status = 'pending', "
                "part_path = CASE WHEN ? THEN part_path ELSE NULL END, "
                "attempts = MAX(attempts - 1, 0), updated_at = ? WHERE status = 'running'",
                (1 if keep_parts else 0, iso()))
        return parts

    async def detach_all_parts(self) -> list[str]:
        """Resume disabled: detach every remembered .part (from any job
        status) and return the paths so the caller can delete them."""
        async with self.db.transaction() as conn:
            rows = await conn.execute_fetchall(
                "SELECT part_path FROM jobs WHERE part_path IS NOT NULL")
            await conn.execute("UPDATE jobs SET part_path = NULL WHERE part_path IS NOT NULL")
        return [r[0] for r in rows]

    async def claim_job(self) -> dict[str, Any] | None:
        """Atomically take the next runnable pending job (counts an attempt)."""
        now = iso()
        row = await self.db.execute_returning(
            "UPDATE jobs SET status = 'running', attempts = attempts + 1, updated_at = ? "
            "WHERE id = (SELECT id FROM jobs WHERE status = 'pending' "
            "AND (not_before IS NULL OR not_before <= ?) ORDER BY priority, id LIMIT 1) "
            "RETURNING *",
            (now, now),
        )
        return dict(row) if row else None

    async def release_job(self, job_id: int, *, keep_part: bool = False) -> None:
        """Give a running job back to the queue without counting the attempt
        (shutdown, FloodWait). ``keep_part``: leave the .part for resuming."""
        await self.db.execute(
            "UPDATE jobs SET status = 'pending', part_path = CASE WHEN ? THEN part_path ELSE NULL END, "
            "attempts = MAX(attempts - 1, 0), updated_at = ? WHERE id = ? AND status = 'running'",
            (1 if keep_part else 0, iso(), job_id))

    async def schedule_retry(self, job_id: int, not_before: datetime, error: str) -> None:
        # part_path is kept: the pipeline decides on the next attempt whether
        # the .part can be resumed (and deletes it if not).
        await self.db.execute(
            "UPDATE jobs SET status = 'pending', not_before = ?, last_error = ?, updated_at = ? "
            "WHERE id = ?", (iso(not_before), error, iso(), job_id))

    async def set_part_path(self, job_id: int, part_path: str | None) -> None:
        await self.db.execute("UPDATE jobs SET part_path = ? WHERE id = ?", (part_path, job_id))

    async def next_retry_time(self) -> datetime | None:
        row = await self.db.fetchone(
            "SELECT MIN(not_before) AS t FROM jobs WHERE status = 'pending' AND not_before > ?", (iso(),))
        return parse_iso(row["t"]) if row and row["t"] else None

    async def job_counts(self) -> dict[str, int]:
        rows = await self.db.fetchall("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")
        return {r["status"]: r["n"] for r in rows}

    # --------------------------------------------------------------- stats
    async def download_stats_since(self, since: datetime) -> tuple[int, int]:
        row = await self.db.fetchone(
            "SELECT COUNT(*) AS n, COALESCE(SUM(file_size), 0) AS total FROM downloads "
            "WHERE downloaded_at >= ?", (iso(since),))
        return (row["n"], row["total"]) if row else (0, 0)

    async def per_chat_stats(self, today_start: datetime, *, limit: int = 50,
                             offset: int = 0) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT d.chat_id, COALESCE(m.chat_title, MAX(d.chat_title)) AS chat_title, "
            "COUNT(*) AS files, COALESCE(SUM(d.file_size), 0) AS bytes, "
            "SUM(CASE WHEN d.downloaded_at >= ? THEN 1 ELSE 0 END) AS today_files, "
            "MAX(d.downloaded_at) AS last_at "
            "FROM downloads d LEFT JOIN monitors m ON m.chat_id = d.chat_id "
            "GROUP BY d.chat_id ORDER BY last_at DESC, d.chat_id LIMIT ? OFFSET ?",
            (iso(today_start), limit, offset))
        return [dict(r) for r in rows]

    async def count_download_chats(self) -> int:
        row = await self.db.fetchone("SELECT COUNT(DISTINCT chat_id) AS n FROM downloads")
        return row["n"] if row else 0

    # ------------------------------------------------------------ backfill
    async def create_backfill(self, chat_id: int, date_from: datetime, date_to: datetime) -> int:
        return await self.db.execute(
            "INSERT INTO backfill_tasks(chat_id, date_from, date_to, status, created_at) "
            "VALUES (?, ?, ?, 'running', ?)", (chat_id, iso(date_from), iso(date_to), iso()))

    async def update_backfill(self, task_id: int, *, scanned: int, enqueued: int,
                              status: str | None = None, error: str | None = None) -> None:
        finished = iso() if status and status != "running" else None
        await self.db.execute(
            "UPDATE backfill_tasks SET scanned = ?, enqueued = ?, status = COALESCE(?, status), "
            "error = COALESCE(?, error), finished_at = COALESCE(?, finished_at) WHERE id = ?",
            (scanned, enqueued, status, error, finished, task_id))

    async def list_backfills(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT b.*, m.chat_title FROM backfill_tasks b LEFT JOIN monitors m USING (chat_id) "
            "ORDER BY b.id DESC LIMIT ? OFFSET ?", (limit, offset))
        return [dict(r) for r in rows]

    async def count_backfills(self) -> int:
        row = await self.db.fetchone("SELECT COUNT(*) AS n FROM backfill_tasks")
        return row["n"] if row else 0

    async def fail_stale_backfills(self) -> None:
        await self.db.execute(
            "UPDATE backfill_tasks SET status = 'cancelled', finished_at = ? WHERE status = 'running'",
            (iso(),))

    async def record_download(self, job: dict[str, Any], *, category: str, file_name: str,
                              file_size: int, saved_path: str) -> None:
        """Insert the download record and mark the job done atomically.

        Must only be called after the file is fully written and renamed.
        """
        try:
            async with self.db.transaction() as conn:
                await conn.execute(
                    "INSERT INTO downloads(chat_id, message_id, media_unique_id, grouped_id, chat_title, "
                    "category, media_kind, file_name, file_size, saved_path, message_date, downloaded_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (job["chat_id"], job["message_id"], job["media_unique_id"], job["grouped_id"],
                     job["chat_title"], category, job["media_kind"], file_name, file_size, saved_path,
                     job["message_date"], iso()),
                )
                await conn.execute(
                    "UPDATE jobs SET status = 'done', last_error = NULL, part_path = NULL, "
                    "updated_at = ? WHERE id = ?",
                    (iso(), job["id"]),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateDownload(str(exc)) from exc

    # ------------------------------------------------------------- history
    async def search_downloads(self, f: HistoryFilter, *, limit: int, offset: int
                               ) -> tuple[list[dict[str, Any]], int]:
        where, params = _where(f, "downloaded_at", category=True)
        total_row = await self.db.fetchone(f"SELECT COUNT(*) AS n FROM downloads{where}", tuple(params))
        rows = await self.db.fetchall(
            f"SELECT * FROM downloads{where} ORDER BY downloaded_at DESC, id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset))
        return [dict(r) for r in rows], (total_row["n"] if total_row else 0)


    async def search_failed_jobs(self, f: HistoryFilter, *, limit: int, offset: int
                                 ) -> tuple[list[dict[str, Any]], int]:
        where, params = _where(f, "updated_at", category=False)
        where = (where + " AND " if where else " WHERE ") + "status = 'failed'"
        total_row = await self.db.fetchone(f"SELECT COUNT(*) AS n FROM jobs{where}", tuple(params))
        rows = await self.db.fetchall(
            f"SELECT * FROM jobs{where} ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset))
        return [dict(r) for r in rows], (total_row["n"] if total_row else 0)


    async def history_chats(self) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT chat_id, MAX(chat_title) AS chat_title FROM ("
            " SELECT chat_id, chat_title FROM downloads UNION ALL"
            " SELECT chat_id, chat_title FROM jobs WHERE status = 'failed' UNION ALL"
            " SELECT chat_id, chat_title FROM monitors) GROUP BY chat_id ORDER BY chat_title COLLATE NOCASE")
        return [dict(r) for r in rows]


    async def retry_job(self, job_id: int) -> str:
        """Put a failed job back in the queue. Returns 'queued', 'skipped'
        (already downloaded / already queued elsewhere) or 'not_failed'."""
        job = await self.get_job(job_id)
        if job is None or job["status"] != "failed":
            return "not_failed"
        dup = await self.find_duplicate(job["chat_id"], job["message_id"], job["media_unique_id"])
        if dup:
            await self.set_job_status(job_id, "skipped", error=dup)
            return "skipped"
        try:
            await self.db.execute(
                "UPDATE jobs SET status = 'pending', attempts = 0, not_before = NULL, last_error = NULL, "
                "source = 'retry', priority = 0, updated_at = ? WHERE id = ? AND status = 'failed'",
                (iso(), job_id))
        except sqlite3.IntegrityError:
            await self.set_job_status(job_id, "skipped", error="queued_message")
            return "skipped"
        return "queued"


    async def failed_job_ids(self) -> list[int]:
        rows = await self.db.fetchall("SELECT id FROM jobs WHERE status = 'failed' ORDER BY id")
        return [r["id"] for r in rows]


    async def get_download(self, download_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM downloads WHERE id = ?", (download_id,))
        return dict(row) if row else None


    async def download_paths(self) -> list[tuple[int, str]]:
        rows = await self.db.fetchall("SELECT id, saved_path FROM downloads")
        return [(r["id"], r["saved_path"]) for r in rows]


    async def requeue_download(self, download_id: int) -> int | None:
        """Remove a download record (its file is gone) and queue the message
        again, atomically. Returns the new job id, or None if not possible."""
        async with self.db.transaction() as conn:
            rows = await conn.execute_fetchall("SELECT * FROM downloads WHERE id = ?", (download_id,))
            if not rows:
                return None
            d = dict(rows[0])
            await conn.execute("DELETE FROM downloads WHERE id = ?", (download_id,))
            now = iso()
            try:
                cur = await conn.execute(
                    "INSERT INTO jobs(chat_id, chat_title, message_id, grouped_id, media_unique_id, "
                    "media_kind, file_name, file_size, message_date, source, status, attempts, priority, "
                    "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?, 'redownload', 'pending', 0, 0, ?, ?)",
                    (d["chat_id"], d["chat_title"], d["message_id"], d["grouped_id"], d["media_unique_id"],
                     d["media_kind"], d["file_name"], d["file_size"], d["message_date"], now, now))
            except sqlite3.IntegrityError:
                raise DuplicateDownload("already queued") from None
            return cur.lastrowid
