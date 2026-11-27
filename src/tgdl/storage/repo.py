"""Data access layer."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .db import Database, iso

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

    async def set_password_hash(self, password_hash: str) -> None:
        async with self.db.transaction() as conn:
            await conn.execute(
                "INSERT INTO admin(id, password_hash, updated_at) VALUES (1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET password_hash=excluded.password_hash, "
                "updated_at=excluded.updated_at",
                (password_hash, iso()),
            )
            # Changing the password invalidates every web session.
            await conn.execute("DELETE FROM web_sessions")

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
                "media_kind, file_name, file_size, message_date, source, status, attempts, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?, 'pending', 0, ?, ?)",
                (job.chat_id, job.chat_title, job.message_id, job.grouped_id, job.media_unique_id,
                 job.media_kind, job.file_name, job.file_size, iso(job.message_date), job.source,
                 now, now),
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

    async def reset_running_jobs(self) -> int:
        """After a crash/restart, running jobs go back to pending."""
        async with self.db.transaction() as conn:
            cur = await conn.execute(
                "UPDATE jobs SET status = 'pending', updated_at = ? WHERE status = 'running'", (iso(),))
            return cur.rowcount

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
                    "UPDATE jobs SET status = 'done', last_error = NULL, updated_at = ? WHERE id = ?",
                    (iso(), job["id"]),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateDownload(str(exc)) from exc
