"""Runtime settings stored in SQLite, validated, cached, hot-reloadable."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import AppConfig
from .core.categories import (
    DEFAULT_MEDIA_TYPES, MEDIA_KINDS, CategoryMap, default_category_map, validate_category_map,
)
from .core.filters import ChatRules
from .core.paths import (
    DEFAULT_PATH_TEMPLATE, resolve_allowed, validate_path_template, validate_prefix_template,
)
from .i18n import SUPPORTED
from .storage.repo import Repository

log = logging.getLogger("tgdl.settings")

MIN_CONCURRENCY = 1
MAX_CONCURRENCY = 10

Listener = Callable[[dict[str, Any]], Awaitable[None] | None]


class SettingsError(ValueError):
    def __init__(self, key: str, code: str, detail: str = "") -> None:
        super().__init__(f"{key}: {code} {detail}".strip())
        self.key = key
        self.code = code
        self.detail = detail


def defaults(cfg: AppConfig) -> dict[str, Any]:
    return {
        "default_dir": str(cfg.allowed_base_dirs[0]),
        "concurrency": 3,
        "path_template": DEFAULT_PATH_TEMPLATE,
        "timezone": "Asia/Taipei",
        "category_map": default_category_map(),
        "default_media_types": list(DEFAULT_MEDIA_TYPES),
        "default_prefix": "",
        "language": "zh-TW",
        "resume_downloads": True,
    }


def validate_setting(cfg: AppConfig, key: str, value: Any) -> Any:
    """Validate and normalize one setting value."""
    try:
        if key == "default_dir":
            return str(resolve_allowed(str(value), cfg.allowed_base_dirs))
        if key == "concurrency":
            n = int(value)
            if not MIN_CONCURRENCY <= n <= MAX_CONCURRENCY:
                raise SettingsError(key, "settings.concurrency_range")
            return n
        if key == "path_template":
            validate_path_template(str(value))
            return str(value)
        if key == "timezone":
            ZoneInfo(str(value))
            return str(value)
        if key == "category_map":
            try:
                return validate_category_map(value)
            except ValueError as exc:
                code, _, detail = str(exc).partition(":")
                raise SettingsError(key, "settings." + code.replace(".", "_"), detail) from exc
        if key == "default_media_types":
            kinds = sorted({str(v) for v in value})
            if any(k not in MEDIA_KINDS for k in kinds):
                raise SettingsError(key, "settings.bad_media_type")
            return kinds
        if key == "default_prefix":
            validate_prefix_template(str(value))
            return str(value)
        if key == "resume_downloads":
            if isinstance(value, bool):
                return value
            if str(value).lower() in ("1", "true", "on", "yes"):
                return True
            if str(value).lower() in ("0", "false", "off", "no", ""):
                return False
            raise SettingsError(key, "settings.invalid")
        if key == "language":
            if value not in SUPPORTED:
                raise SettingsError(key, "settings.bad_language")
            return value
    except SettingsError:
        raise
    except ZoneInfoNotFoundError as exc:
        raise SettingsError(key, "settings.bad_timezone") from exc
    except (ValueError, TypeError) as exc:
        code = getattr(exc, "code", None) or "settings.invalid"
        raise SettingsError(key, code) from exc
    raise SettingsError(key, "settings.unknown_key")


class SettingsService:
    def __init__(self, cfg: AppConfig, repo: Repository) -> None:
        self.cfg = cfg
        self.repo = repo
        self._values: dict[str, Any] = defaults(cfg)
        self._listeners: list[Listener] = []

    async def load(self) -> None:
        stored = await self.repo.load_settings()
        for key, value in stored.items():
            if key not in self._values:
                continue
            try:
                self._values[key] = validate_setting(self.cfg, key, value)
            except SettingsError as exc:
                log.warning("Ignoring invalid stored setting %s (%s); using default", key, exc.code)

    def get(self, key: str) -> Any:
        return self._values[key]

    def all(self) -> dict[str, Any]:
        return dict(self._values)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self._values["timezone"])

    @property
    def category_map(self) -> CategoryMap:
        return self._values["category_map"]

    def on_change(self, listener: Listener) -> None:
        self._listeners.append(listener)

    async def update(self, changes: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
        """Validate all changes first, persist, then notify listeners.
        Returns {key: (old, new)} for keys that actually changed."""
        normalized = {k: validate_setting(self.cfg, k, v) for k, v in changes.items()}
        diff = {k: (self._values[k], v) for k, v in normalized.items() if self._values[k] != v}
        if not diff:
            return {}
        await self.repo.save_settings({k: new for k, (_, new) in diff.items()})
        for k, (_, new) in diff.items():
            self._values[k] = new
        for listener in list(self._listeners):
            result = listener(diff)
            if result is not None:
                await result
        return diff

    def rules_for(self, monitor: dict[str, Any] | None) -> ChatRules:
        """Effective rules for a chat: global defaults + monitor overrides.

        The download directory is re-validated against the allowed roots on
        every call (a stored value can never escape them).
        """
        m = monitor or {}
        raw_dir = m.get("download_dir") or self._values["default_dir"]
        download_dir = resolve_allowed(raw_dir, self.cfg.allowed_base_dirs)
        media_types = m.get("media_types")
        prefix = m.get("filename_prefix")
        return ChatRules(
            media_types=frozenset(media_types if media_types is not None
                                  else self._values["default_media_types"]),
            download_dir=Path(download_dir),
            prefix=prefix if prefix is not None else self._values["default_prefix"],
            min_size=m.get("min_size"),
            max_size=m.get("max_size"),
            keyword=m.get("keyword") or None,
        )
