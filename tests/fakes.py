"""Stand-ins for Telethon objects used by tests."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any


def make_message(msg_id: int, *, kind: str = "document", media_id: int | None = None,
                 name: str | None = "file.bin", size: int = 1000, mime: str = "application/octet-stream",
                 grouped_id: int | None = None,
                 date: datetime = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)) -> Any:
    media_id = media_id if media_id is not None else msg_id * 10
    m = SimpleNamespace(id=msg_id, date=date, grouped_id=grouped_id, web_preview=None,
                        photo=None, document=None, sticker=None, voice=None, video_note=None,
                        gif=None, audio=None, video=None)
    if kind == "photo":
        m.photo = SimpleNamespace(id=media_id)
        m.file = SimpleNamespace(name=None, size=size, mime_type="image/jpeg", ext=".jpg")
    else:
        m.document = SimpleNamespace(id=media_id, size=size)
        m.file = SimpleNamespace(name=name, size=size, mime_type=mime, ext=None)
        if kind != "document":
            setattr(m, {"animation": "gif"}.get(kind, kind), True)
    return m


@dataclass
class FakeClient:
    """download_media writes ``payload`` (or ``sizes[msg_id]`` bytes) into the file."""
    payload: bytes = b"x" * 1000
    fail_with: BaseException | None = None
    calls: list[int] = field(default_factory=list)

    async def download_media(self, message: Any, file: Any, progress_callback: Any = None) -> Any:
        self.calls.append(message.id)
        file.write(self.payload[: len(self.payload) // 2])
        if self.fail_with is not None:
            raise self.fail_with
        file.write(self.payload[len(self.payload) // 2:])
        if progress_callback:
            progress_callback(len(self.payload), len(self.payload))
        return file
