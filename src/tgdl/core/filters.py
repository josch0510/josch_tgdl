"""Per-chat effective settings and media filters."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .media import MediaInfo


@dataclass(frozen=True)
class ChatRules:
    """Effective settings for one chat (global defaults + overrides)."""

    media_types: frozenset[str]
    download_dir: Path
    prefix: str = ""
    min_size: int | None = None
    max_size: int | None = None
    keyword: str | None = None


def filter_reason(info: MediaInfo, rules: ChatRules) -> str | None:
    """Return a skip reason, or None if the media should be downloaded."""
    if info.kind not in rules.media_types:
        return "type_disabled"
    if info.file_size is not None:
        if rules.min_size is not None and info.file_size < rules.min_size:
            return "too_small"
        if rules.max_size is not None and info.file_size > rules.max_size:
            return "too_large"
    if rules.keyword:
        if rules.keyword.casefold() not in info.file_name.casefold():
            return "keyword_mismatch"
    return None
