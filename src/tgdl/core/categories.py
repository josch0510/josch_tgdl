"""Media category classification."""

from __future__ import annotations

import copy
import re
from typing import Any

CATEGORIES: tuple[str, ...] = (
    "Photos", "Videos", "Audio", "Voice", "Documents", "Archives", "Others",
)
MEDIA_KINDS: tuple[str, ...] = (
    "photo", "video", "video_note", "animation", "audio", "voice", "document", "sticker",
)
DEFAULT_MEDIA_TYPES: tuple[str, ...] = tuple(k for k in MEDIA_KINDS if k != "sticker")

CategoryMap = dict[str, Any]

DEFAULT_CATEGORY_MAP: CategoryMap = {
    # Telegram media kind -> category ("document" is resolved by extension/MIME)
    "kinds": {
        "photo": "Photos",
        "video": "Videos",
        "video_note": "Videos",
        "animation": "Videos",
        "audio": "Audio",
        "voice": "Voice",
        "sticker": "Others",
    },
    # category -> extensions (lowercase, no dot)
    "extensions": {
        "Photos": ["jpg", "jpeg", "png", "gif", "webp", "heic", "heif", "bmp", "tif", "tiff", "avif"],
        "Videos": ["mp4", "mkv", "mov", "avi", "webm", "m4v", "flv", "wmv", "ts", "3gp", "mpg", "mpeg"],
        "Audio": ["mp3", "flac", "m4a", "aac", "ogg", "wav", "opus", "wma", "alac", "ape"],
        "Voice": [],
        "Documents": ["pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "odp",
                      "txt", "md", "csv", "rtf", "epub", "mobi", "azw3", "json", "xml"],
        "Archives": ["zip", "rar", "7z", "tar", "gz", "tgz", "bz2", "xz", "zst", "iso", "cab"],
        "Others": [],
    },
    # MIME prefix -> category (longest prefix wins)
    "mime": {
        "image/": "Photos",
        "video/": "Videos",
        "audio/": "Audio",
        "application/pdf": "Documents",
        "text/": "Documents",
        "application/zip": "Archives",
        "application/x-7z-compressed": "Archives",
        "application/vnd.rar": "Archives",
        "application/x-rar-compressed": "Archives",
        "application/gzip": "Archives",
        "application/x-tar": "Archives",
    },
}


def default_category_map() -> CategoryMap:
    return copy.deepcopy(DEFAULT_CATEGORY_MAP)


def file_ext(filename: str | None) -> str:
    if not filename or "." not in filename:
        return ""
    return filename.rsplit(".", 1)[1].strip().lower()


_NUMBERED = re.compile(r"^\d{1,4}$")          # .7z.001, .zip.002, .rar.003
_SPLIT_ALIASES = (
    (re.compile(r"^z\d{2,3}$"), "zip"),       # .z01, .z02 (split zip)
    (re.compile(r"^r\d{2,3}$"), "rar"),       # .r00, .r01 (old-style split rar)
)


def ext_candidates(filename: str | None) -> list[str]:
    """Extensions to look up, most specific first.

    Split archives are mapped to the underlying format, so they land in the
    same category as the complete archive:
    ``a.7z.001`` -> ["001", "7z"], ``a.z01`` -> ["z01", "zip"],
    ``a.r00`` -> ["r00", "rar"]. An extension listed explicitly in the
    mapping (e.g. "001") still takes precedence.
    """
    ext = file_ext(filename)
    if not ext:
        return []
    out = [ext]
    if _NUMBERED.match(ext):
        inner = file_ext((filename or "").rsplit(".", 1)[0])
        if inner and not _NUMBERED.match(inner):
            out.append(inner)
    for pattern, alias in _SPLIT_ALIASES:
        if pattern.match(ext):
            out.append(alias)
    return out


def classify(kind: str, mime_type: str | None, filename: str | None,
             mapping: CategoryMap | None = None) -> str:
    """Classify by Telegram media kind, then file extension (split archive
    parts resolve to their archive format), then MIME type."""
    m = mapping or DEFAULT_CATEGORY_MAP
    kinds: dict[str, str] = m.get("kinds", {})
    if kind != "document" and kind in kinds:
        return kinds[kind]
    for ext in ext_candidates(filename):
        for category, exts in m.get("extensions", {}).items():
            if ext in exts:
                return category
    if mime_type:
        mime = mime_type.lower()
        for prefix in sorted(m.get("mime", {}), key=len, reverse=True):
            if mime.startswith(prefix):
                return m["mime"][prefix]
    return "Others"


def validate_category_map(raw: Any) -> CategoryMap:
    """Validate and normalize a user-supplied mapping. Raises ValueError."""
    if not isinstance(raw, dict):
        raise ValueError("category_map.not_object")
    out: CategoryMap = {"kinds": {}, "extensions": {c: [] for c in CATEGORIES}, "mime": {}}

    for kind, cat in (raw.get("kinds") or {}).items():
        if kind not in MEDIA_KINDS or kind == "document":
            raise ValueError(f"category_map.bad_kind:{kind}")
        if cat not in CATEGORIES:
            raise ValueError(f"category_map.bad_category:{cat}")
        out["kinds"][kind] = cat

    seen: dict[str, str] = {}
    for cat, exts in (raw.get("extensions") or {}).items():
        if cat not in CATEGORIES:
            raise ValueError(f"category_map.bad_category:{cat}")
        if not isinstance(exts, list):
            raise ValueError("category_map.bad_extensions")
        for e in exts:
            ext = str(e).strip().lower().lstrip(".")
            if not ext or not ext.isascii() or not ext.replace("-", "").replace("_", "").isalnum():
                raise ValueError(f"category_map.bad_extension:{e}")
            if ext in seen and seen[ext] != cat:
                raise ValueError(f"category_map.duplicate_extension:{ext}")
            if ext not in seen:
                seen[ext] = cat
                out["extensions"][cat].append(ext)

    for prefix, cat in (raw.get("mime") or {}).items():
        p = str(prefix).strip().lower()
        if not p or "/" not in p:
            raise ValueError(f"category_map.bad_mime:{prefix}")
        if cat not in CATEGORIES:
            raise ValueError(f"category_map.bad_category:{cat}")
        out["mime"][p] = cat
    return out
