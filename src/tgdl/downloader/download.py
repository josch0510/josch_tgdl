"""Low-level file download: write to .part, verify size, atomic rename."""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ..core.paths import unique_path

ProgressCallback = Callable[[int, int], Any]

# .part files currently being written (cleaned up on shutdown).
ACTIVE_PARTS: set[Path] = set()


class SizeMismatch(Exception):
    def __init__(self, expected: int | None, actual: int) -> None:
        super().__init__(f"size mismatch: expected={expected} actual={actual}")
        self.expected = expected
        self.actual = actual


async def download_to_file(client: Any, message: Any, target: Path, *,
                           expected_size: int | None,
                           progress: ProgressCallback | None = None,
                           on_part: Callable[[Path], Awaitable[None]] | None = None,
                           ) -> tuple[Path, int]:
    """Download ``message``'s media to ``target`` (or ``target_N`` if taken).

    The data goes to ``<final>.part`` first; it is renamed only after the
    size has been verified. On any error or cancellation the .part file is
    removed. Returns (final_path, size).
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    final = unique_path(target)
    part = final.with_name(final.name + ".part")
    ACTIVE_PARTS.add(part)
    try:
        if on_part is not None:
            await on_part(part)
        with open(part, "xb") as fh:  # "x": never overwrite an existing file
            await client.download_media(message, file=fh, progress_callback=progress)
            fh.flush()
            os.fsync(fh.fileno())
        size = part.stat().st_size
        if size == 0 or (expected_size is not None and size != expected_size):
            raise SizeMismatch(expected_size, size)
        if final.exists():  # appeared while downloading
            final = unique_path(final)
        os.replace(part, final)
        return final, size
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    finally:
        ACTIVE_PARTS.discard(part)


def cleanup_parts() -> int:
    """Remove all in-progress .part files. Returns how many were removed."""
    removed = 0
    for part in list(ACTIVE_PARTS):
        try:
            part.unlink()
            removed += 1
        except FileNotFoundError:
            pass
        ACTIVE_PARTS.discard(part)
    return removed
