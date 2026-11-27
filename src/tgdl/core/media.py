"""Extract downloadable media information from a Telethon Message.

Uses duck typing (only the attributes Telethon exposes) so it can be unit
tested with simple stand-in objects.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .categories import file_ext


@dataclass(frozen=True)
class MediaInfo:
    kind: str            # one of categories.MEDIA_KINDS
    unique_id: str       # "photo:<id>" / "doc:<id>" - stable across forwards
    file_name: str
    file_size: int | None
    mime_type: str | None

    @property
    def ext(self) -> str:
        return file_ext(self.file_name)


def media_kind(message: Any) -> str | None:
    """Return the media kind of a message, or None if nothing downloadable.

    Web page previews, polls, geo, contacts, etc. are ignored.
    """
    if getattr(message, "photo", None) is not None and getattr(message, "web_preview", None) is None:
        return "photo"
    if getattr(message, "document", None) is None or getattr(message, "web_preview", None) is not None:
        return None
    for attr, kind in (("sticker", "sticker"), ("voice", "voice"), ("video_note", "video_note"),
                       ("gif", "animation"), ("audio", "audio"), ("video", "video")):
        if getattr(message, attr, None):
            return kind
    return "document"


def _default_ext(kind: str, mime: str | None) -> str:
    if kind == "photo":
        return ".jpg"
    if mime:
        guessed = mimetypes.guess_extension(mime, strict=False)
        if guessed:
            return guessed
    return {"voice": ".ogg", "video_note": ".mp4", "animation": ".mp4", "video": ".mp4",
            "audio": ".mp3", "sticker": ".webp"}.get(kind, ".bin")


def extract_media(message: Any) -> MediaInfo | None:
    kind = media_kind(message)
    if kind is None:
        return None
    f = getattr(message, "file", None)
    mime = getattr(f, "mime_type", None) if f is not None else None
    size = getattr(f, "size", None) if f is not None else None
    name = getattr(f, "name", None) if f is not None else None

    if kind == "photo":
        unique_id = f"photo:{message.photo.id}"
        mime = mime or "image/jpeg"
    else:
        unique_id = f"doc:{message.document.id}"
        size = getattr(message.document, "size", size)

    if not name:
        date: datetime = getattr(message, "date", None) or datetime.now(timezone.utc)
        ext = (getattr(f, "ext", None) if f is not None else None) or _default_ext(kind, mime)
        name = f"{kind}_{date.strftime('%Y%m%d_%H%M%S')}{ext}"
    return MediaInfo(kind=kind, unique_id=unique_id, file_name=name,
                     file_size=int(size) if size is not None else None, mime_type=mime)
