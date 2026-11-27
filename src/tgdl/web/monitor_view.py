"""Effective settings of a monitor, for display (dashboard + monitors page)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..core.categories import MEDIA_KINDS
from ..core.paths import PathNotAllowed

if TYPE_CHECKING:
    from ..app import App


@dataclass(frozen=True)
class MonitorView:
    chat_id: int
    chat_title: str
    chat_type: str
    enabled: bool
    media_types: list[str]        # effective, in canonical order
    types_custom: bool
    download_dir: str             # effective directory
    dir_custom: bool
    dir_error: str | None         # i18n key when the stored dir is no longer allowed
    prefix: str                   # effective prefix template ("" = none)
    prefix_custom: bool
    min_size: int | None
    max_size: int | None
    keyword: str | None

    @property
    def has_filters(self) -> bool:
        return self.min_size is not None or self.max_size is not None or bool(self.keyword)


def describe(app: "App", m: dict[str, Any]) -> MonitorView:
    defaults = app.settings.all()
    try:
        rules = app.settings.rules_for(m)
        types = set(rules.media_types)
        directory, dir_error = str(rules.download_dir), None
        prefix = rules.prefix
    except PathNotAllowed as exc:
        # e.g. ALLOWED_BASE_DIRS changed after this directory was chosen
        types = set(m["media_types"] if m["media_types"] is not None else defaults["default_media_types"])
        directory, dir_error = str(m.get("download_dir") or defaults["default_dir"]), exc.code
        prefix = m["filename_prefix"] if m["filename_prefix"] is not None else defaults["default_prefix"]
    return MonitorView(
        chat_id=m["chat_id"], chat_title=m["chat_title"], chat_type=m["chat_type"],
        enabled=bool(m["enabled"]),
        media_types=[k for k in MEDIA_KINDS if k in types], types_custom=m["media_types"] is not None,
        download_dir=directory, dir_custom=bool(m.get("download_dir")), dir_error=dir_error,
        prefix=prefix, prefix_custom=m["filename_prefix"] is not None,
        min_size=m.get("min_size"), max_size=m.get("max_size"), keyword=m.get("keyword") or None,
    )
