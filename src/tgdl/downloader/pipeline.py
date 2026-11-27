"""Decide whether a message should be downloaded, and execute one job."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import AppConfig
from ..core.categories import classify
from ..core.filters import ChatRules, filter_reason
from ..core.media import MediaInfo, extract_media
from ..core.paths import PathContext, render_path, resolve_allowed
from ..logging_setup import log_event
from ..settings import SettingsService
from ..storage.db import parse_iso
from ..storage.repo import DuplicateDownload, NewJob, Repository
from .download import ProgressCallback, download_to_file

log = logging.getLogger("tgdl.downloader")


class MediaGone(Exception):
    """The message or its media no longer exists."""


class Pipeline:
    def __init__(self, cfg: AppConfig, repo: Repository, settings: SettingsService) -> None:
        self.cfg = cfg
        self.repo = repo
        self.settings = settings

    async def consider(self, *, chat_id: int, chat_title: str, message: Any, source: str,
                       monitor: dict[str, Any] | None) -> tuple[int | None, str | None]:
        """Filter + dedup + enqueue. Returns (job_id, None) or (None, reason)."""
        info = extract_media(message)
        if info is None:
            return None, "no_media"
        rules = self.settings.rules_for(monitor)
        reason = filter_reason(info, rules)
        if reason:
            return None, reason
        date: datetime = getattr(message, "date", None) or datetime.now(timezone.utc)
        job_id, reason = await self.repo.enqueue(NewJob(
            chat_id=chat_id, chat_title=chat_title, message_id=message.id,
            grouped_id=getattr(message, "grouped_id", None), media_unique_id=info.unique_id,
            media_kind=info.kind, file_name=info.file_name, file_size=info.file_size,
            message_date=date, source=source, priority=10 if source == "backfill" else 0,
        ))
        level = logging.INFO if job_id else logging.DEBUG
        log_event(log, level, "enqueue" if job_id else "skip", f"file={info.file_name}",
                  chat=chat_id, msg_id=message.id, reason=reason)
        return job_id, reason

    def target_for(self, job: dict[str, Any], info: MediaInfo, rules: ChatRules) -> tuple[str, Path]:
        category = classify(info.kind, info.mime_type, info.file_name, self.settings.category_map)
        ctx = PathContext(
            base_dir=rules.download_dir,
            chat_name=job["chat_title"] or str(job["chat_id"]),
            chat_id=job["chat_id"],
            category=category,
            msg_id=job["message_id"],
            filename=info.file_name,
            message_date=parse_iso(job["message_date"]),
            tz=self.settings.tz,
            grouped_id=job["grouped_id"],
            prefix_template=rules.prefix,
        )
        path = render_path(self.settings.get("path_template"), ctx)
        # Final guard: the rendered file path must stay inside an allowed root.
        return category, resolve_allowed(path, self.cfg.allowed_base_dirs)

    async def execute(self, client: Any, job: dict[str, Any], message: Any, *,
                      progress: ProgressCallback | None = None) -> Path | None:
        """Download one job that the caller already marked ``running``.

        ``message`` must be freshly fetched (valid file_reference). Returns the
        saved path, or None if skipped."""
        info = extract_media(message) if message is not None else None
        if info is None:
            raise MediaGone()
        dup = await self.repo.find_duplicate(job["chat_id"], job["message_id"], info.unique_id,
                                             include_queue=False)
        if dup:
            await self.repo.set_job_status(job["id"], "skipped", error=dup)
            log_event(log, logging.INFO, "skip", chat=job["chat_id"], msg_id=job["message_id"], reason=dup)
            return None

        rules = self.settings.rules_for(await self.repo.get_monitor(job["chat_id"]))
        category, target = self.target_for(job, info, rules)
        log_event(log, logging.INFO, "download_start", f"file={info.file_name} size={info.file_size}",
                  chat=job["chat_id"], msg_id=job["message_id"])

        expected = info.file_size if info.kind != "photo" else None
        async def remember_part(part: Path) -> None:
            await self.repo.set_part_path(job["id"], str(part))

        final, size = await download_to_file(client, message, target, expected_size=expected,
                                             progress=progress, on_part=remember_part)
        try:
            await self.repo.record_download(job, category=category, file_name=final.name,
                                            file_size=size, saved_path=str(final))
        except DuplicateDownload:
            final.unlink(missing_ok=True)
            await self.repo.set_job_status(job["id"], "skipped", error="duplicate_race")
            log_event(log, logging.INFO, "skip", chat=job["chat_id"], msg_id=job["message_id"],
                      reason="duplicate_race")
            return None
        log_event(log, logging.INFO, "download_done", f"size={size} path={final}",
                  chat=job["chat_id"], msg_id=job["message_id"])
        return final
