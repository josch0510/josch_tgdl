"""Logging: rotating English log file, separate audit log, secret redaction,
and an in-memory ring buffer the web log viewer can read."""

from __future__ import annotations

import collections
import logging
import os
import re
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from .config import ensure_private_file

LOG_FORMAT = ("%(asctime)s | %(levelname)s | chat=%(chat)s | msg_id=%(msg_id)s | "
              "action=%(action)s | reason=%(reason)s | %(message)s")
JOURNAL_FORMAT = ("%(levelname)s | chat=%(chat)s | msg_id=%(msg_id)s | action=%(action)s | "
                  "reason=%(reason)s | %(message)s")
AUDIT_FORMAT = "%(asctime)s | %(levelname)s | user=%(user)s | ip=%(ip)s | action=%(action)s | %(message)s"
MAX_BYTES = 10 * 1024 * 1024
BACKUPS = 5

_FIELDS = ("chat", "msg_id", "action", "reason")
_AUDIT_FIELDS = ("user", "ip", "action")
_PHONE_RE = re.compile(r"\+\d[\d \-]{6,18}\d")


class _Defaults(logging.Filter):
    def __init__(self, fields: tuple[str, ...]) -> None:
        super().__init__()
        self.fields = fields

    def filter(self, record: logging.LogRecord) -> bool:
        for f in self.fields:
            if not hasattr(record, f):
                setattr(record, f, "-")
        return True


class RedactFilter(logging.Filter):
    """Removes registered secrets (API hash, phone, ...) and anything that
    looks like an international phone number from log messages."""

    _secrets: set[str] = set()
    _lock = threading.Lock()

    @classmethod
    def register(cls, *secrets: str | None) -> None:
        with cls._lock:
            for s in secrets:
                if s and len(s) >= 4:
                    cls._secrets.add(s)

    @classmethod
    def redact(cls, text: str) -> str:
        for s in cls._secrets:
            if s in text:
                text = text.replace(s, "[REDACTED]")
        return _PHONE_RE.sub("[REDACTED]", text)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # pragma: no cover - malformed log call
            msg = str(record.msg)
        record.msg = self.redact(msg)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self.redact(record.exc_text)
        return True


class RingBufferHandler(logging.Handler):
    """Keeps the most recent formatted records for the web log viewer and
    notifies subscribers (used for SSE)."""

    def __init__(self, capacity: int = 2000) -> None:
        super().__init__()
        self.records: collections.deque[dict[str, Any]] = collections.deque(maxlen=capacity)
        self.listeners: list[Any] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                "ts": record.created,
                "level": record.levelname,
                "levelno": record.levelno,
                "logger": record.name,
                "line": self.format(record),
            }
            self.records.append(entry)
            for cb in list(self.listeners):
                try:
                    cb(entry)
                except Exception:  # pragma: no cover
                    pass
        except Exception:  # pragma: no cover
            self.handleError(record)


ring_buffer = RingBufferHandler()


def _file_handler(path: Path, fmt: str, fields: tuple[str, ...]) -> RotatingFileHandler:
    h = RotatingFileHandler(path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8")
    ensure_private_file(path)
    h.setFormatter(logging.Formatter(fmt))
    h.addFilter(_Defaults(fields))
    h.addFilter(RedactFilter())
    return h


def setup_logging(logs_dir: Path, *, console: bool = True, level: int = logging.INFO) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)

    app = logging.getLogger("tgdl")
    app.setLevel(level)
    app.propagate = False
    for h in list(app.handlers):
        app.removeHandler(h)
    app.addHandler(_file_handler(logs_dir / "tgdl.log", LOG_FORMAT, _FIELDS))
    ring_buffer.setFormatter(logging.Formatter(LOG_FORMAT))
    ring_buffer.filters.clear()
    ring_buffer.addFilter(_Defaults(_FIELDS))
    ring_buffer.addFilter(RedactFilter())
    app.addHandler(ring_buffer)
    if console:
        ch = logging.StreamHandler()
        # journald stamps every line itself; avoid a second timestamp there
        journal = bool(os.environ.get("JOURNAL_STREAM"))
        ch.setFormatter(logging.Formatter(JOURNAL_FORMAT if journal else LOG_FORMAT))
        ch.addFilter(_Defaults(_FIELDS))
        ch.addFilter(RedactFilter())
        ch.setLevel(logging.INFO)
        app.addHandler(ch)

    # Telethon's own logs go to the same file, warnings and above only.
    tl = logging.getLogger("telethon")
    tl.setLevel(logging.WARNING)
    tl.propagate = False
    for h in list(tl.handlers):
        tl.removeHandler(h)
    tl.addHandler(app.handlers[0])

    audit = logging.getLogger("tgdl_audit")
    audit.setLevel(logging.INFO)
    audit.propagate = False
    for h in list(audit.handlers):
        audit.removeHandler(h)
    audit.addHandler(_file_handler(logs_dir / "audit.log", AUDIT_FORMAT, _AUDIT_FIELDS))


def log_event(logger: logging.Logger, level: int, action: str, message: str = "", *,
              chat: object = None, msg_id: object = None, reason: object = None) -> None:
    logger.log(level, message, extra={
        "chat": chat if chat is not None else "-",
        "msg_id": msg_id if msg_id is not None else "-",
        "action": action,
        "reason": reason if reason is not None else "-",
    })


def audit(action: str, message: str = "", *, user: str = "-", ip: str = "-",
          level: int = logging.INFO) -> None:
    logging.getLogger("tgdl_audit").log(level, message, extra={"user": user, "ip": ip, "action": action})
