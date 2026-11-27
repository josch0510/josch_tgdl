"""Low-level file download: write to .part, verify size, atomic rename,
optionally resume an interrupted .part."""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ..core.paths import unique_path

ProgressCallback = Callable[[int, int], Any]

# Resume offsets are aligned to this. Telegram's upload.getFile requires
# offset % limit == 0 and requests that do not cross a 1 MiB boundary; with
# 512 KiB requests and 512 KiB-aligned offsets both always hold. At most the
# last 512 KiB already on disk is fetched again.
RESUME_ALIGN = 512 * 1024

# .part files currently being written (cleaned up on shutdown when resume is off).
ACTIVE_PARTS: set[Path] = set()


class SizeMismatch(Exception):
    def __init__(self, expected: int | None, actual: int) -> None:
        super().__init__(f"size mismatch: expected={expected} actual={actual}")
        self.expected = expected
        self.actual = actual


def resume_offset(part: Path, expected_size: int | None) -> int | None:
    """Where an existing .part can be resumed from, or None (part unusable
    and deleted). The tail after the last aligned boundary is discarded: it
    may be a partly written chunk after a crash."""
    try:
        size = part.stat().st_size
    except FileNotFoundError:
        return None
    if not part.is_file() or expected_size is None or size > expected_size:
        part.unlink(missing_ok=True)
        return None
    offset = size - size % RESUME_ALIGN
    if offset <= 0:
        part.unlink(missing_ok=True)
        return None
    return offset


def _final_of(part: Path) -> Path:
    return part.with_name(part.name[: -len(".part")])


async def download_to_file(client: Any, message: Any, target: Path, *,
                           expected_size: int | None,
                           progress: ProgressCallback | None = None,
                           on_part: Callable[[Path], Awaitable[None]] | None = None,
                           resume_from: Path | None = None,
                           keep_part: Callable[[BaseException], bool] | None = None,
                           on_resume: Callable[[int], Any] | None = None,
                           ) -> tuple[Path, int]:
    """Download ``message``'s media to ``target`` (or ``target_N`` if taken).

    The data goes to ``<final>.part`` first; it is renamed only after the
    size has been verified. With ``resume_from`` (an existing .part of the
    same file) the download continues from that file's aligned size.

    On error or cancellation the .part is removed unless ``keep_part(exc)``
    returns True (resume enabled). Returns (final_path, size).
    """
    offset = resume_offset(resume_from, expected_size) if resume_from is not None else None
    if offset is not None and resume_from is not None:
        part, final = resume_from, _final_of(resume_from)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        final = unique_path(target)
        part = final.with_name(final.name + ".part")
    ACTIVE_PARTS.add(part)
    try:
        if on_part is not None:
            await on_part(part)
        if offset is not None:
            if on_resume is not None:
                on_resume(offset)
            with open(part, "r+b") as fh:
                fh.seek(offset)
                fh.truncate()
                done = offset
                if progress is not None:
                    progress(done, expected_size or 0)
                async for chunk in client.iter_download(
                        message.document, offset=offset, request_size=RESUME_ALIGN,
                        file_size=expected_size):
                    fh.write(chunk)
                    done += len(chunk)
                    if progress is not None:
                        progress(done, expected_size or 0)
                fh.flush()
                os.fsync(fh.fileno())
        else:
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
    except BaseException as exc:
        if keep_part is None or not keep_part(exc):
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
