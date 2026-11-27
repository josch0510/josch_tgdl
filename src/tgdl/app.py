"""`tgdl serve`: wire everything onto one asyncio loop and run until Ctrl+C."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .config import AppConfig
from .core.paths import PathNotAllowed, resolve_allowed
from .downloader.download import cleanup_parts
from .downloader.flood import FLOOD_ERRORS, FloodGate
from .downloader.pipeline import Pipeline
from .downloader.queue import JobQueue
from .downloader.worker import WorkerPool
from .events import EventBus
from .logging_setup import log_event
from .settings import SettingsService
from .storage.db import Database
from .storage.repo import Repository
from .telegram.backfill import BackfillManager
from .telegram.client import DialogCache
from .telegram.monitor import MonitorManager

log = logging.getLogger("tgdl.app")

Translate = Callable[..., str]
MONITOR_RESYNC_SECONDS = 30


@dataclass
class App:
    """Everything the web layer needs, in one place."""

    cfg: AppConfig
    client: Any
    db: Database
    repo: Repository
    settings: SettingsService
    bus: EventBus
    download_gate: FloodGate
    api_gate: FloodGate
    pipeline: Pipeline
    queue: JobQueue
    pool: WorkerPool
    monitors: MonitorManager
    backfill: BackfillManager
    dialogs: DialogCache
    me_name: str = ""
    started_at: float = field(default_factory=time.time)
    shutting_down: asyncio.Event = field(default_factory=asyncio.Event)
    web_server: Any = None
    web_task: asyncio.Task[Any] | None = None

    @classmethod
    def build(cls, cfg: AppConfig, client: Any, db: Database, repo: Repository,
              settings: SettingsService) -> "App":
        bus = EventBus()
        download_gate = FloodGate("download", bus)
        api_gate = FloodGate("api", bus)
        pipeline = Pipeline(cfg, repo, settings)
        queue = JobQueue(repo, pipeline, bus)
        pool = WorkerPool(client, repo, settings, pipeline, queue, download_gate, bus)
        monitors = MonitorManager(client, repo, queue, api_gate, bus)
        backfill = BackfillManager(client, repo, settings, queue, api_gate, bus)
        dialogs = DialogCache(client, api_gate)
        return cls(cfg, client, db, repo, settings, bus, download_gate, api_gate, pipeline,
                   queue, pool, monitors, backfill, dialogs)

    def flood_status(self) -> list[dict[str, Any]]:
        return [self.download_gate.status(), self.api_gate.status()]


async def remove_stale_parts(cfg: AppConfig, parts: list[str]) -> int:
    """Delete .part files left by a crash (only inside allowed roots)."""
    removed = 0
    for raw in parts:
        try:
            p = resolve_allowed(raw, cfg.allowed_base_dirs)
        except PathNotAllowed:
            continue
        if p.name.endswith(".part") and p.is_file():
            p.unlink(missing_ok=True)
            removed += 1
    return removed


async def warm_entity_cache(app: App) -> None:
    """Load dialogs once so chat ids can be resolved; tolerate FloodWait."""
    for _ in range(3):
        await app.api_gate.wait()
        try:
            await app.dialogs.get(refresh=True)
            return
        except FLOOD_ERRORS as exc:
            app.api_gate.trigger(getattr(exc, "seconds", 60))
        except Exception as exc:  # noqa: BLE001
            log_event(log, logging.WARNING, "dialogs_failed", repr(exc), reason=type(exc).__name__)
            return


async def monitor_resync(app: App) -> None:
    """Pick up monitor changes made by other processes (e.g. the CLI)."""
    while True:
        await asyncio.sleep(MONITOR_RESYNC_SECONDS)
        with contextlib.suppress(Exception):
            await app.monitors.load()


async def stop_web(app: App) -> None:
    if app.web_server is not None:
        app.web_server.should_exit = True
    if app.web_task is not None:
        try:
            await asyncio.wait_for(asyncio.shield(app.web_task), timeout=5)
        except (TimeoutError, Exception):  # noqa: BLE001
            app.web_task.cancel()
            await asyncio.gather(app.web_task, return_exceptions=True)


async def shutdown(app: App) -> None:
    log_event(log, logging.INFO, "shutdown", "stopping: no new jobs accepted")
    app.shutting_down.set()
    await stop_web(app)
    app.queue.accepting = False
    await app.monitors.detach()
    await app.backfill.stop_all()
    await app.pool.stop()
    removed = cleanup_parts()
    log_event(log, logging.INFO, "shutdown", f"workers stopped, removed {removed} .part file(s)")


async def run_server(cfg: AppConfig, t: Translate, args: argparse.Namespace) -> int:
    from telethon import utils

    from .cli import open_storage
    from .telegram.client import build_client

    client = build_client(cfg, long_running=True)
    db, repo, settings = await open_storage(cfg)
    app: App | None = None
    extra_tasks: list[asyncio.Task[Any]] = []
    try:
        print(t("cli.serve.connecting"))
        await client.connect()
        if not await client.is_user_authorized():
            print(t("cli.not_logged_in"))
            return 1
        me = await client.get_me()

        app = App.build(cfg, client, db, repo, settings)
        app.me_name = utils.get_display_name(me)
        stale = await repo.reset_running_jobs()
        if stale:
            await remove_stale_parts(cfg, stale)
        await repo.fail_stale_backfills()
        await app.monitors.load()
        app.monitors.attach()
        await warm_entity_cache(app)
        app.pool.start()
        extra_tasks.append(asyncio.create_task(monitor_resync(app), name="monitor-resync"))

        from .web.server import WebBindError, start_web

        try:
            app.web_task = await start_web(app)
        except WebBindError as exc:
            print(t("cli.serve.bind_failed", error=str(exc)))
            return 1
        extra_tasks.append(app.web_task)

        log_event(log, logging.INFO, "serve_start",
                  f"monitors={app.monitors.enabled_count}/{len(app.monitors.monitors)} "
                  f"concurrency={settings.get('concurrency')}")
        print(t("cli.serve.started", name=app.me_name, monitors=app.monitors.enabled_count,
                concurrency=settings.get("concurrency")))
        print(t("cli.serve.web_url", url=f"http://{cfg.web_host}:{cfg.web_port}/"))
        if await repo.get_password_hash() is None:
            print(t("cli.serve.no_password"))

        stop = asyncio.Event()
        presses = 0

        def on_signal() -> None:
            nonlocal presses
            presses += 1
            if presses == 1:
                print("\n" + t("cli.serve.stopping"))
                stop.set()
            else:
                print(t("cli.serve.force_quit"))
                os._exit(130)

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, on_signal)

        waiters = {asyncio.ensure_future(stop.wait()), asyncio.ensure_future(client.disconnected)}
        waiters.update(extra_tasks)
        done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        for d in done:
            if d is not None and d.done() and not d.cancelled() and d.exception():
                log_event(log, logging.ERROR, "serve_error", repr(d.exception()))
        for w in waiters:
            if w not in extra_tasks:
                w.cancel()
        return 0
    finally:
        if app is not None:
            await shutdown(app)
        for task in extra_tasks:
            task.cancel()
        await asyncio.gather(*extra_tasks, return_exceptions=True)
        await client.disconnect()
        await db.close()
        print(t("cli.serve.stopped"))

