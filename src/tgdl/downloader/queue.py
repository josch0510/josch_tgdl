"""Persistent job queue (SQLite-backed) with in-memory wakeups."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from ..events import EventBus
from ..storage.repo import Repository
from .pipeline import Pipeline

# Even without notifications, idle workers re-check the DB this often
# (picks up retries whose backoff expired and jobs added by other processes).
POLL_INTERVAL = 5.0


class JobQueue:
    def __init__(self, repo: Repository, pipeline: Pipeline, bus: EventBus) -> None:
        self.repo = repo
        self.pipeline = pipeline
        self.bus = bus
        self.accepting = True
        self._cond = asyncio.Condition()

    async def notify(self) -> None:
        async with self._cond:
            self._cond.notify_all()

    async def submit(self, *, chat_id: int, chat_title: str, message: Any, source: str,
                     monitor: dict[str, Any] | None) -> tuple[int | None, str | None]:
        """Filter, dedupe and persist a job; wakes a worker."""
        if not self.accepting:
            return None, "shutting_down"
        job_id, reason = await self.pipeline.consider(
            chat_id=chat_id, chat_title=chat_title, message=message, source=source, monitor=monitor)
        if job_id is not None:
            await self.notify()
            await self.publish_counts()
        return job_id, reason

    async def publish_counts(self) -> None:
        self.bus.publish("queue", **(await self.repo.job_counts()))

    async def _wait_timeout(self) -> float:
        nxt = await self.repo.next_retry_time()
        if nxt is None:
            return POLL_INTERVAL
        delta = (nxt - datetime.now(timezone.utc)).total_seconds()
        return max(0.05, min(POLL_INTERVAL, delta))

    async def next_job(self, should_stop: Any) -> dict[str, Any] | None:
        """Block until a job can be claimed, or return None when
        ``should_stop()`` becomes true (checked on every wakeup)."""
        while not should_stop():
            job = await self.repo.claim_job()
            if job is not None:
                return job
            timeout = await self._wait_timeout()
            async with self._cond:
                try:
                    await asyncio.wait_for(self._cond.wait(), timeout)
                except TimeoutError:
                    pass
        return None
