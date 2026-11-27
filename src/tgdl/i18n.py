"""Minimal JSON-based i18n with zh-TW -> en fallback."""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

SUPPORTED: tuple[str, ...] = ("en", "zh-TW")
FALLBACK = "en"
LOCALES_DIR = Path(__file__).parent / "locales"

log = logging.getLogger("tgdl.i18n")


class _SafeDict(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


class Translator:
    def __init__(self, locales_dir: Path = LOCALES_DIR) -> None:
        self.catalogs: dict[str, dict[str, str]] = {}
        for lang in SUPPORTED:
            path = locales_dir / f"{lang}.json"
            with path.open(encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict) or not all(isinstance(v, str) for v in data.values()):
                raise ValueError(f"Locale file {path} must be a flat JSON object of strings")
            self.catalogs[lang] = data
        self._warned: set[str] = set()

    def t(self, key: str, lang: str = FALLBACK, /, **params: Any) -> str:
        text = self.catalogs.get(lang, {}).get(key)
        if text is None:
            text = self.catalogs[FALLBACK].get(key)
        if text is None:
            if key not in self._warned:
                self._warned.add(key)
                log.warning("Missing translation key %s", key)
            return key
        return text.format_map(_SafeDict(params)) if params else text

    def has(self, key: str) -> bool:
        return key in self.catalogs[FALLBACK]


@lru_cache(maxsize=1)
def get_translator() -> Translator:
    return Translator()


def normalize_lang(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip().replace("_", "-").lower()
    if v.startswith("zh"):
        # zh, zh-tw, zh-hant, zh-hk -> Traditional Chinese
        return "zh-TW"
    if v.startswith("en"):
        return "en"
    return None


def from_accept_language(header: str | None) -> str | None:
    if not header:
        return None
    ranked: list[tuple[float, str]] = []
    for part in header.split(","):
        bits = part.strip().split(";")
        q = 1.0
        for b in bits[1:]:
            b = b.strip()
            if b.startswith("q="):
                try:
                    q = float(b[2:])
                except ValueError:
                    q = 0.0
        ranked.append((q, bits[0]))
    for _, tag in sorted(ranked, key=lambda x: -x[0]):
        lang = normalize_lang(tag)
        if lang:
            return lang
    return None


def cli_lang(flag: str | None) -> str:
    return (normalize_lang(flag) or normalize_lang(os.environ.get("TGDL_LANG"))
            or normalize_lang(os.environ.get("LANG")) or FALLBACK)
