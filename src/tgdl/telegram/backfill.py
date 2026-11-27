"""Backfill: scan a chat's history in a date range and enqueue media."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from ..downloader.flood import FLOOD_ERRORS, FloodGate
from ..downloader.queue import JobQueue
from ..events import EventBus
from ..logging_setup import log_event
from ..settings import SettingsService
from ..storage.repo import Repository

log = logging.getLogger("tgdl.backfill")

PROGRESS_EVERY = 50  # messages between progress updates


class BackfillError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def local_range_to_utc(date_from: date, date_to: date, tz: Any) -> tuple[datetime, datetime]:
    """Inclusive local-date range -> [start, end) in UTC."""
    if date_from > date_to:
        raise BackfillError("backfill.bad_range")
    start = datetime.combine(date_from, time.min, tz).astimezone(timezone.utc)
    end = datetime.combine(date_to + timedelta(days=1), time.min, tz).astimezone(timezone.utc)
    return start, end


class BackfillManager:
    def __init__(self, client: Any, repo: Repository, settings: SettingsService, queue: JobQueue,
                 api_gate: FloodGate, bus: EventBus) -> None:
        self.client = client
        self.repo = repo
        self.settings = settings
        self.queue = queue
        self.api_gate = api_gate
        self.bus = bus
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._by_chat: dict[int, int] = {}
        self.progress: dict[int, dict[str, Any]] = {}

    def running_for(self, chat_id: int) -> int | None:
        return self._by_chat.get(chat_id)

    async def start(self, chat_id: int, date_from: date, date_to: date) -> int:
        if chat_id in self._by_chat:
            raise BackfillError("backfill.already_running")
        monitor = await self.repo.get_monitor(chat_id)
        if monitor is None:
            raise BackfillError("backfill.not_monitored")
        start, end = local_range_to_utc(date_from, date_to, self.settings.tz)
        task_id = await self.repo.create_backfill(chat_id, start, end)
        self._by_chat[chat_id] = task_id
        self.progress[task_id] = {"task_id": task_id, "chat_id": chat_id,
                                  "chat_title": monitor["chat_title"], "scanned": 0,
                                  "enqueued": 0, "status": "running",
                                  "date_from": date_from.isoformat(), "date_to": date_to.isoformat()}
        self._tasks[task_id] = asyncio.create_task(self._run(task_id, monitor, start, end),
                                                   name=f"backfill-{task_id}")
        log_event(log, logging.INFO, "backfill_start", f"from={date_from} to={date_to}", chat=chat_id)
        return task_id

    async def cancel(self, task_id: int) -> bool:
        task = self._tasks.get(task_id)
        if task is None:
            return False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        # A task cancelled before it started never ran its finally block.
        if task_id in self._tasks:
            await self._finalize(task_id, "cancelled", None)
        return True

    async def _finalize(self, task_id: int, status: str, error: str | None) -> None:
        p = self.progress[task_id]
        p["status"] = status
        self._tasks.pop(task_id, None)
        self._by_chat.pop(p["chat_id"], None)
        await self.repo.update_backfill(task_id, scanned=p["scanned"], enqueued=p["enqueued"],
                                        status=status, error=error)
        self.bus.publish("backfill", **p)
        log_event(log, logging.INFO, "backfill_end",
                  f"status={status} scanned={p['scanned']} enqueued={p['enqueued']}",
                  chat=p["chat_id"])

    async def stop_all(self) -> None:
        for task_id in list(self._tasks):
            await self.cancel(task_id)

    async def _run(self, task_id: int, monitor: dict[str, Any], start: datetime, end: datetime) -> None:
        chat_id = monitor["chat_id"]
        p = self.progress[task_id]
        status, error = "done", None
        last_id = 0
        try:
            while True:
                await self.api_gate.wait()
                kwargs: dict[str, Any] = {"offset_id": last_id} if last_id else {"offset_date": end}
                try:
                    async for m in self.client.iter_messages(chat_id, **kwargs):
                        if m.date < start:
                            break
                        last_id = m.id
                        p["scanned"] += 1
                        if getattr(m, "media", None) is not None:
                            if not self.queue.accepting:
                                raise asyncio.CancelledError()
                            jid, _ = await self.queue.submit(chat_id=chat_id,
                                                             chat_title=monitor["chat_title"],
                                                             message=m, source="backfill",
                                                             monitor=monitor)
                            if jid:
                                p["enqueued"] += 1
                        if p["scanned"] % PROGRESS_EVERY == 0:
                            await self.repo.update_backfill(task_id, scanned=p["scanned"],
                                                            enqueued=p["enqueued"])
                            self.bus.publish("backfill", **p)
                    break
                except FLOOD_ERRORS as exc:
                    # Resume from last_id once the gate opens.
                    self.api_gate.trigger(getattr(exc, "seconds", 60), chat=chat_id)
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001
            status, error = "failed", type(exc).__name__
            log_event(log, logging.ERROR, "backfill_failed", repr(exc), chat=chat_id, reason=error)
        finally:
            await asyncio.shield(self._finalize(task_id, status, error))
