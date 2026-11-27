"""FloodGate: pause a class of Telegram requests after a FloodWaitError."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from telethon.errors import FloodPremiumWaitError, FloodWaitError

from ..events import EventBus
from ..logging_setup import log_event

log = logging.getLogger("tgdl.flood")

# Both carry ``.seconds``; FloodPremiumWaitError is not a FloodWaitError subclass.
FLOOD_ERRORS: tuple[type[Exception], ...] = (FloodWaitError, FloodPremiumWaitError)

# Extra margin on top of what Telegram asks for.
SAFETY_MARGIN = 1.0


class FloodGate:
    """While closed, :meth:`wait` blocks. Telegram rate limits are per
    account, so one gate pauses all workers that share it."""

    def __init__(self, scope: str, bus: EventBus | None = None,
                 clock: Any = time.monotonic, wall: Any = time.time) -> None:
        self.scope = scope
        self.bus = bus
        self._clock = clock
        self._wall = wall
        self._until = 0.0       # monotonic
        self._until_wall = 0.0  # epoch seconds, for the UI
        self.total_triggers = 0

    @property
    def remaining(self) -> float:
        return max(0.0, self._until - self._clock())

    @property
    def active(self) -> bool:
        return self.remaining > 0

    def trigger(self, seconds: float, *, chat: object = None, msg_id: object = None) -> None:
        until = self._clock() + seconds + SAFETY_MARGIN
        self.total_triggers += 1
        if until > self._until:
            self._until = until
            self._until_wall = self._wall() + seconds + SAFETY_MARGIN
        log_event(log, logging.WARNING, "flood_wait", f"scope={self.scope} seconds={seconds}",
                  chat=chat, msg_id=msg_id, reason="FloodWaitError")
        if self.bus:
            self.bus.publish("flood", **self.status())

    async def wait(self) -> None:
        was_active = self.active
        while (rem := self.remaining) > 0:
            await asyncio.sleep(min(rem, 5.0))
        if was_active and self.bus:
            self.bus.publish("flood", **self.status())

    def status(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "active": self.active,
            "remaining": round(self.remaining),
            "until": self._until_wall if self.active else None,
        }
