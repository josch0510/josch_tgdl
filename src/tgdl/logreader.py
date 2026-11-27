"""Read the tail of a log file efficiently and filter entries by level."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

LEVELS: tuple[str, ...] = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_ENTRY_START = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
MAX_TAIL_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class LogEntry:
    text: str          # may contain several lines (tracebacks)
    level: str | None

    @property
    def level_no(self) -> int:
        return LEVELS.index(self.level) if self.level in LEVELS else 0


def tail_lines(path: Path, n: int, *, max_bytes: int = MAX_TAIL_BYTES) -> list[str]:
    """Return up to the last ``n`` lines of ``path`` without reading the
    whole file."""
    if n <= 0 or not path.exists():
        return []
    block = 64 * 1024
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        end = fh.tell()
        pos, data = end, b""
        while pos > 0 and data.count(b"\n") <= n and end - pos < max_bytes:
            step = min(block, pos)
            pos -= step
            fh.seek(pos)
            data = fh.read(step) + data
    lines = data.decode("utf-8", errors="replace").splitlines()
    if pos > 0 and lines:
        lines = lines[1:]  # first line is probably partial
    return lines[-n:]


def parse_entries(lines: list[str]) -> list[LogEntry]:
    entries: list[LogEntry] = []
    for line in lines:
        if _ENTRY_START.match(line) or not entries:
            parts = line.split(" | ", 2)
            level = parts[1].strip() if len(parts) > 1 and parts[1].strip() in LEVELS else None
            entries.append(LogEntry(line, level))
        else:  # continuation (traceback)
            prev = entries[-1]
            entries[-1] = LogEntry(prev.text + "\n" + line, prev.level)
    return entries


def read_log(path: Path, *, lines: int, min_level: str = "DEBUG", query: str = "") -> list[LogEntry]:
    min_no = LEVELS.index(min_level) if min_level in LEVELS else 0
    q = query.casefold()
    out = [e for e in parse_entries(tail_lines(path, lines))
           if (e.level is None or e.level_no >= min_no) and (not q or q in e.text.casefold())]
    return out
