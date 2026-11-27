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
