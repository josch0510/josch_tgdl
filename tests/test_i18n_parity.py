import json
import re
from pathlib import Path

from tgdl.i18n import LOCALES_DIR, SUPPORTED, Translator, from_accept_language, normalize_lang

PLACEHOLDER = re.compile(r"\{(\w+)\}")


def load(lang: str) -> dict[str, str]:
    return json.loads((LOCALES_DIR / f"{lang}.json").read_text(encoding="utf-8"))


def test_key_parity() -> None:
    en, zh = load("en"), load("zh-TW")
    assert set(en) - set(zh) == set(), "keys missing in zh-TW"
    assert set(zh) - set(en) == set(), "keys only in zh-TW"


def test_placeholder_parity() -> None:
    en, zh = load("en"), load("zh-TW")
    for key, text in en.items():
        assert set(PLACEHOLDER.findall(text)) == set(PLACEHOLDER.findall(zh[key])), key


def test_no_empty_values() -> None:
    for lang in SUPPORTED:
        for key, value in load(lang).items():
            assert value.strip(), f"{lang}:{key} is empty"


def test_fallback_to_en(tmp_path: Path) -> None:
    (tmp_path / "en.json").write_text(json.dumps({"a": "A {x}", "b": "B"}), encoding="utf-8")
    (tmp_path / "zh-TW.json").write_text(json.dumps({"a": "甲 {x}"}), encoding="utf-8")
    tr = Translator(tmp_path)
    assert tr.t("a", "zh-TW", x=1) == "甲 1"
    assert tr.t("b", "zh-TW") == "B"
    assert tr.t("missing", "zh-TW") == "missing"
    assert tr.t("a", "en") == "A {x}"


def test_lang_normalization() -> None:
    assert normalize_lang("zh_TW.UTF-8") == "zh-TW"
    assert normalize_lang("zh-Hant") == "zh-TW"
    assert normalize_lang("en_US.UTF-8") == "en"
    assert normalize_lang("fr") is None
    assert from_accept_language("fr;q=0.9, zh-TW;q=0.8, en;q=0.5") == "zh-TW"
    assert from_accept_language("en-US,en;q=0.9") == "en"


def test_every_template_key_is_translated() -> None:
    """Literal t("...") keys in templates and Python must exist in en.json,
    and every dynamic key family must be complete."""
    from tgdl.core.categories import CATEGORIES, MEDIA_KINDS
    from tgdl.settings import defaults

    en = load("en")
    root = LOCALES_DIR.parent
    pattern = re.compile(r"""\bt\(\s*["']([a-z_]+\.[A-Za-z0-9_.\-]+)["']""")
    missing = set()
    files = list((root / "web" / "templates").rglob("*.html")) + list(root.rglob("*.py"))
    for f in files:
        for key in pattern.findall(f.read_text(encoding="utf-8")):
            if not key.endswith(("_", ".")) and key not in en:
                missing.add(f"{f.name}:{key}")
    # keys passed around as values: error="...", ok="..." / err="..." flashes
    for f in (root / "web").rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        for key in re.findall(r'(?:error|fail)\(?=?\s*"([a-z_]+\.[a-z_]+)"', text):
            if key not in en:
                missing.add(f"{f.name}:{key}")
        for key in re.findall(r'(?:ok|err)="([a-z_]+)"', text):
            if f"flash.{key}" not in en:
                missing.add(f"{f.name}:flash.{key}")
    families = {
        "kind": MEDIA_KINDS, "cat": CATEGORIES, "chat_type": ("channel", "group", "user"),
        "flood.scope": ("download", "api"), "lang": SUPPORTED,
        "backfill.status": ("running", "done", "failed", "cancelled"),
    }
    for prefix, items in families.items():
        sep = "_" if prefix in ("flood.scope", "backfill.status") else "."
        for item in items:
            if f"{prefix}{sep}{item}" not in en:
                missing.add(f"{prefix}{sep}{item}")
    from tgdl.config import AppConfig
    for key in defaults(AppConfig(home=Path("/"), api_id=None, api_hash=None, allowed_base_dirs=(Path("/"),))):
        if f"settings.field_{key}" not in en:
            missing.add(f"settings.field_{key}")
    for code in ("not_absolute", "outside_allowed", "invalid"):
        assert f"dir.{code}" in en
    assert not missing, sorted(missing)
