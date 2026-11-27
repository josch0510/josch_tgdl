"""Download worker pool: concurrency adjustable at runtime, FloodWait
pausing, retry with exponential backoff, graceful shutdown."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from telethon.errors import (
    FileReferenceExpiredError, RPCError, ServerError, TimedOutError,
)

from ..core.paths import PathNotAllowed, TemplateError
from ..events import EventBus
from ..logging_setup import log_event
from ..settings import SettingsService
from ..storage.repo import Repository
from .download import SizeMismatch
from .flood import FLOOD_ERRORS, FloodGate
from .pipeline import MediaGone, Pipeline
from .queue import JobQueue

log = logging.getLogger("tgdl.worker")

MAX_RETRIES = 3          # retries after the first attempt
BACKOFF_BASE = 5.0       # seconds: 5, 10, 20
PROGRESS_INTERVAL = 0.5  # seconds between progress events per download

TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    ConnectionError, TimeoutError, asyncio.TimeoutError, TimedOutError, ServerError,
    FileReferenceExpiredError, SizeMismatch,
)
PERMANENT_ERRORS: tuple[type[BaseException], ...] = (MediaGone, PathNotAllowed, TemplateError)


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, PERMANENT_ERRORS):
        return False
    if isinstance(exc, TRANSIENT_ERRORS):
        return True
    # Telegram internal errors (-500, 500, 503...) are worth retrying.
    code = getattr(exc, "code", None)
    return isinstance(exc, RPCError) and isinstance(code, int) and (code >= 500 or code < 0)


def backoff_delay(attempt: int) -> float:
    """Delay before retry number ``attempt`` (1-based)."""
    return BACKOFF_BASE * (2 ** (attempt - 1))


@dataclass
class ActiveDownload:
    job_id: int
    worker_id: int
    chat_id: int
    chat_title: str
    message_id: int
    file_name: str
    total: int
    done: int = 0
    speed: float = 0.0  # bytes/s (smoothed)
    started: float = field(default_factory=time.time)
    state: str = "downloading"  # downloading / flood_wait
    _last_t: float = field(default_factory=time.monotonic, repr=False)
    _last_done: int = field(default=0, repr=False)
    _last_pub: float = field(default=0.0, repr=False)

    def public(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if not k.startswith("_")}


class MessageFetcher:
    """Fetches a fresh message (valid file_reference) for a job."""

    def __init__(self, client: Any) -> None:
        self.client = client
        self._dialogs_loaded = False

    async def get(self, chat_id: int, message_id: int) -> Any:
        try:
            return await self.client.get_messages(chat_id, ids=message_id)
        except ValueError:
            # Entity not in the session cache yet: load dialogs once and retry.
            if self._dialogs_loaded:
                raise
            self._dialogs_loaded = True
            await self.client.get_dialogs()
            return await self.client.get_messages(chat_id, ids=message_id)


class WorkerPool:
    def __init__(self, client: Any, repo: Repository, settings: SettingsService,
                 pipeline: Pipeline, queue: JobQueue, gate: FloodGate, bus: EventBus) -> None:
        self.client = client
        self.repo = repo
        self.settings = settings
        self.pipeline = pipeline
        self.queue = queue
        self.gate = gate
        self.bus = bus
        self.fetcher = MessageFetcher(client)
        self.active: dict[int, ActiveDownload] = {}
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._retire: set[int] = set()
        self._next_id = 1
        self._stopping = False
        self.target = int(settings.get("concurrency"))
        settings.on_change(self._on_settings)

    # --------------------------------------------------------- lifecycle
    def start(self) -> None:
        self.resize(self.target)

    async def _on_settings(self, diff: dict[str, Any]) -> None:
        if "concurrency" in diff:
            self.resize(int(diff["concurrency"][1]))
            await self.queue.notify()

    def resize(self, n: int) -> None:
        """Grow immediately; shrink by retiring workers after their current
        file (downloads are never interrupted by a resize)."""
        self.target = n
        alive = [wid for wid in sorted(self._tasks) if wid not in self._retire]
        if len(alive) < n:
            for _ in range(n - len(alive)):
                wid = self._next_id
                self._next_id += 1
                self._tasks[wid] = asyncio.create_task(self._worker(wid), name=f"worker-{wid}")
        elif len(alive) > n:
            # Prefer retiring idle workers.
            busy = {a.worker_id for a in self.active.values()}
            victims = sorted(alive, key=lambda w: (w in busy, -w))[: len(alive) - n]
            self._retire.update(victims)
        log_event(log, logging.INFO, "pool_resize", f"concurrency={n}")
        self.bus.publish("pool", **self.status())

    async def stop(self) -> None:
        """Cancel all workers. Interrupted jobs return to the queue and their
        .part files are removed by the downloader."""
        self._stopping = True
        tasks = list(self._tasks.values())
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    def status(self) -> dict[str, Any]:
        return {
            "concurrency": self.target,
            "workers": len(self._tasks) - len(self._retire & set(self._tasks)),
            "active": [a.public() for a in self.active.values()],
            "flood": self.gate.status(),
        }

    # ------------------------------------------------------------ worker
    def _should_stop(self, wid: int) -> bool:
        return self._stopping or wid in self._retire

    async def _worker(self, wid: int) -> None:
        try:
            while not self._should_stop(wid):
                await self.gate.wait()
                job = await self.queue.next_job(lambda: self._should_stop(wid))
                if job is None:
                    break
                try:
                    await self._run(wid, job)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - a worker must never die
                    log_event(log, logging.ERROR, "worker_error", repr(exc),
                              chat=job.get("chat_id"), msg_id=job.get("message_id"),
                              reason=type(exc).__name__)
                    await asyncio.sleep(1)
        finally:
            self._tasks.pop(wid, None)
            self._retire.discard(wid)

    def _progress_cb(self, ad: ActiveDownload) -> Any:
        def cb(done: int, total: int) -> None:
            now = time.monotonic()
            ad.done = done
            if total:
                ad.total = total
            dt = now - ad._last_t
            if dt >= PROGRESS_INTERVAL:
                inst = (done - ad._last_done) / dt
                ad.speed = inst if ad.speed == 0 else ad.speed * 0.7 + inst * 0.3
                ad._last_t, ad._last_done = now, done
            if now - ad._last_pub >= PROGRESS_INTERVAL or done == ad.total:
                ad._last_pub = now
                self.bus.publish("progress", **ad.public())
        return cb

    async def _run(self, wid: int, job: dict[str, Any]) -> None:
        jid, chat_id, msg_id = job["id"], job["chat_id"], job["message_id"]
        ad = ActiveDownload(job_id=jid, worker_id=wid, chat_id=chat_id,
                            chat_title=job["chat_title"] or str(chat_id), message_id=msg_id,
                            file_name=job["file_name"] or "", total=job["file_size"] or 0)
        self.active[jid] = ad
        self.bus.publish("job_start", **ad.public())
        outcome = "failed"
        try:
            message = await self.fetcher.get(chat_id, msg_id)
            path = await self.pipeline.execute(self.client, job, message,
                                               progress=self._progress_cb(ad))
            outcome = "done" if path else "skipped"
        except FLOOD_ERRORS as exc:
            outcome = "requeued"
            await self.repo.release_job(jid)
            self.gate.trigger(getattr(exc, "seconds", 60), chat=chat_id, msg_id=msg_id)
        except asyncio.CancelledError:
            outcome = "requeued"
            await asyncio.shield(self.repo.release_job(jid))
            raise
        except Exception as exc:  # noqa: BLE001 - classified below
            reason = type(exc).__name__
            attempts = int(job["attempts"])
            if is_transient(exc) and attempts <= MAX_RETRIES:
                outcome = "retry"
                delay = backoff_delay(attempts)
                await self.repo.schedule_retry(
                    jid, datetime.now(timezone.utc) + timedelta(seconds=delay), reason)
                log_event(log, logging.WARNING, "download_retry",
                          f"attempt={attempts} retry_in={delay:.0f}s error={exc!r}",
                          chat=chat_id, msg_id=msg_id, reason=reason)
            else:
                await self.repo.set_job_status(jid, "failed", error=reason)
                log_event(log, logging.ERROR, "download_failed", f"attempts={attempts} error={exc!r}",
                          chat=chat_id, msg_id=msg_id, reason=reason)
        finally:
            self.active.pop(jid, None)
            self.bus.publish("job_end", job_id=jid, outcome=outcome, chat_id=chat_id,
                             message_id=msg_id, file_name=ad.file_name)
            if not self._stopping:
                await self.queue.publish_counts()
