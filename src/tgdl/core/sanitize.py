"""Filename / path component sanitizing.

All functions are pure and work on single path components (never on full
paths): a component returned from here never contains a path separator,
is never "." or "..", and is at most ``max_bytes`` long in UTF-8.
"""

from __future__ import annotations

import re
import unicodedata

MAX_COMPONENT_BYTES = 255
REPLACEMENT = "_"
# Longest extension (in bytes, without the dot) we try to keep on truncation.
MAX_EXT_BYTES = 16

_FORBIDDEN = re.compile(r'[/\\:*?"<>|]')
_WHITESPACE = re.compile(r"\s+")
# Bidi overrides / marks can make a filename display differently from what it is.
_BIDI = dict.fromkeys(
    map(ord, "‎‏‪‫‬‭‮⁦⁧⁨⁩"),
    None,
)


def truncate_utf8(value: str, max_bytes: int) -> str:
    """Truncate ``value`` to at most ``max_bytes`` UTF-8 bytes without
    splitting a multi-byte character."""
    if max_bytes <= 0:
        return ""
    raw = value.encode("utf-8")
    if len(raw) <= max_bytes:
        return value
    return raw[:max_bytes].decode("utf-8", errors="ignore")


def _clean(value: str) -> str:
    s = unicodedata.normalize("NFC", value)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Cc")
    s = s.translate(_BIDI)
    s = _FORBIDDEN.sub(REPLACEMENT, s)
    s = _WHITESPACE.sub(" ", s)
    # Leading dots would create hidden files; trailing dots/spaces are
    # problematic on many filesystems.
    return s.strip(" .")


def sanitize_component(value: object, *, max_bytes: int = MAX_COMPONENT_BYTES,
                       empty: str = REPLACEMENT) -> str:
    """Sanitize a directory name (or any single path component)."""
    s = _clean(str(value))
    s = truncate_utf8(s, max_bytes).rstrip(" .")
    return s if s else empty


def split_ext(name: str) -> tuple[str, str]:
    """Split ``name`` into (stem, ".ext"). Only short, space-free
    extensions are treated as extensions."""
    stem, dot, ext = name.rpartition(".")
    if dot and stem and ext and " " not in ext and len(ext.encode("utf-8")) <= MAX_EXT_BYTES:
        return stem, "." + ext
    return name, ""


def sanitize_filename(value: object, *, max_bytes: int = MAX_COMPONENT_BYTES) -> str:
    """Sanitize a file name, keeping its extension when truncating."""
    s = _clean(str(value))
    if not s:
        return REPLACEMENT
    if len(s.encode("utf-8")) <= max_bytes:
        return s
    stem, ext = split_ext(s)
    stem = truncate_utf8(stem, max_bytes - len(ext.encode("utf-8"))).rstrip(" .")
    return (stem or REPLACEMENT) + ext
