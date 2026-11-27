"""Live chat monitors: one NewMessage handler, in-memory enabled set."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from telethon import events

from ..downloader.flood import FLOOD_ERRORS, FloodGate
from ..downloader.queue import JobQueue
from ..events import EventBus
from ..logging_setup import log_event
from ..storage.repo import Repository
from .client import ChatInfo

log = logging.getLogger("tgdl.monitor")

# Telegram delivers album items as separate updates, occasionally not all of
# them. After seeing one item we re-read the neighbourhood to catch the rest.
ALBUM_SWEEP_DELAY = 3.0
ALBUM_WINDOW = 10  # albums have at most 10 items


class MonitorManager:
    def __init__(self, client: Any, repo: Repository, queue: JobQueue, api_gate: FloodGate,
                 bus: EventBus) -> None:
        self.client = client
        self.repo = repo
        self.queue = queue
        self.api_gate = api_gate
        self.bus = bus
        self.monitors: dict[int, dict[str, Any]] = {}
        self._album_tasks: dict[tuple[int, int], asyncio.Task[None]] = {}
        self._attached = False

    # ------------------------------------------------------------ state
    async def load(self) -> None:
        self.monitors = {m["chat_id"]: m for m in await self.repo.list_monitors()}

    async def reload(self, chat_id: int) -> None:
        """Re-read one monitor after it was changed (web UI)."""
        m = await self.repo.get_monitor(chat_id)
        if m is None:
            self.monitors.pop(chat_id, None)
        else:
            self.monitors[chat_id] = m
        self.bus.publish("monitors", enabled=self.enabled_count, total=len(self.monitors))

    @property
    def enabled_count(self) -> int:
        return sum(1 for m in self.monitors.values() if m["enabled"])

    async def add(self, chat: ChatInfo) -> None:
        await self.repo.add_monitor(chat.chat_id, chat.title, chat.chat_type)
        await self.reload(chat.chat_id)
        log_event(log, logging.INFO, "monitor_add", f"type={chat.chat_type}", chat=chat.chat_id)

    async def remove(self, chat_id: int) -> None:
        await self.repo.delete_monitor(chat_id)
        await self.reload(chat_id)
        log_event(log, logging.INFO, "monitor_remove", chat=chat_id)

    async def set_enabled(self, chat_id: int, enabled: bool) -> None:
        await self.repo.update_monitor(chat_id, enabled=enabled)
        await self.reload(chat_id)
        log_event(log, logging.INFO, "monitor_enable" if enabled else "monitor_disable", chat=chat_id)

    # --------------------------------------------------------- handlers
    def attach(self) -> None:
        if not self._attached:
            self.client.add_event_handler(self._on_new_message, events.NewMessage())
            self._attached = True

    async def detach(self) -> None:
        if self._attached:
            self.client.remove_event_handler(self._on_new_message)
            self._attached = False
        tasks = list(self._album_tasks.values())
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._album_tasks.clear()

    async def _on_new_message(self, event: Any) -> None:
        try:
            await self.handle_message(event.chat_id, event.message)
        except Exception as exc:  # noqa: BLE001 - never let a handler crash Telethon's loop
            log_event(log, logging.ERROR, "monitor_error", repr(exc), chat=event.chat_id,
                      msg_id=getattr(event.message, "id", None), reason=type(exc).__name__)

    async def handle_message(self, chat_id: int, message: Any) -> int | None:
        monitor = self.monitors.get(chat_id)
        if monitor is None or not monitor["enabled"]:
            return None
        if getattr(message, "media", None) is None:
            return None
        job_id, _ = await self.queue.submit(chat_id=chat_id, chat_title=monitor["chat_title"],
                                            message=message, source="live", monitor=monitor)
        grouped_id = getattr(message, "grouped_id", None)
        if grouped_id:
            key = (chat_id, grouped_id)
            if key not in self._album_tasks:
                self._album_tasks[key] = asyncio.create_task(
                    self._album_sweep(chat_id, grouped_id, message.id))
        return job_id

    async def _album_sweep(self, chat_id: int, grouped_id: int, anchor_id: int) -> None:
        try:
            await asyncio.sleep(ALBUM_SWEEP_DELAY)
            await self.api_gate.wait()
            monitor = self.monitors.get(chat_id)
            if monitor is None or not monitor["enabled"]:
                return
            ids = list(range(max(1, anchor_id - ALBUM_WINDOW), anchor_id + ALBUM_WINDOW + 1))
            messages = await self.client.get_messages(chat_id, ids=ids)
            found = 0
            for m in messages:
                if m is not None and m.grouped_id == grouped_id:
                    jid, _ = await self.queue.submit(chat_id=chat_id, chat_title=monitor["chat_title"],
                                                     message=m, source="live", monitor=monitor)
                    found += 1 if jid else 0
            if found:
                log_event(log, logging.INFO, "album_sweep", f"grouped_id={grouped_id} added={found}",
                          chat=chat_id)
        except FLOOD_ERRORS as exc:
            self.api_gate.trigger(getattr(exc, "seconds", 60), chat=chat_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log_event(log, logging.WARNING, "album_sweep_failed", repr(exc), chat=chat_id,
                      reason=type(exc).__name__)
        finally:
            self._album_tasks.pop((chat_id, grouped_id), None)
