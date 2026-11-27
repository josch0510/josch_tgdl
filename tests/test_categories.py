import pytest

from tgdl.core.categories import (
    CATEGORIES, DEFAULT_CATEGORY_MAP, classify, default_category_map, validate_category_map,
)


@pytest.mark.parametrize("kind, mime, name, expected", [
    ("photo", "image/jpeg", "a.jpg", "Photos"),
    ("video", "video/mp4", "a.mp4", "Videos"),
    ("video_note", "video/mp4", None, "Videos"),
    ("animation", "video/mp4", "a.mp4", "Videos"),
    ("audio", "audio/mpeg", "song.mp3", "Audio"),
    ("voice", "audio/ogg", "voice.ogg", "Voice"),          # kind wins over mime/ext
    ("sticker", "image/webp", "s.webp", "Others"),
    ("document", "application/zip", "a.zip", "Archives"),
    ("document", "application/octet-stream", "a.RAR", "Archives"),  # ext, case-insensitive
    ("document", "application/pdf", "report.pdf", "Documents"),
    ("document", "image/png", "screenshot.png", "Photos"),
    ("document", "video/x-matroska", "movie.mkv", "Videos"),
    ("document", "audio/flac", "track", "Audio"),         # no ext -> MIME
    ("document", "application/x-7z-compressed", "noext", "Archives"),
    ("document", "application/octet-stream", "unknown.xyz", "Others"),
    ("document", None, None, "Others"),
])
def test_classify_defaults(kind, mime, name, expected) -> None:
    assert classify(kind, mime, name) == expected


def test_extension_beats_mime() -> None:
    # A .zip mislabeled as octet-stream or as text still goes to Archives
    assert classify("document", "text/plain", "x.zip") == "Archives"


def test_custom_mapping() -> None:
    m = default_category_map()
    m["extensions"]["Documents"].append("apk")
    m["kinds"]["sticker"] = "Photos"
    m = validate_category_map(m)
    assert classify("document", None, "app.apk", m) == "Documents"
    assert classify("sticker", "image/webp", "s.webp", m) == "Photos"


def test_default_map_is_valid() -> None:
    assert validate_category_map(DEFAULT_CATEGORY_MAP)["kinds"] == DEFAULT_CATEGORY_MAP["kinds"]
    for cat in DEFAULT_CATEGORY_MAP["extensions"]:
        assert cat in CATEGORIES


def test_extensions_normalized() -> None:
    m = validate_category_map({"extensions": {"Archives": [".ZIP", "Rar"]}})
    assert m["extensions"]["Archives"] == ["zip", "rar"]


@pytest.mark.parametrize("bad", [
    [],
    {"kinds": {"photo": "Pictures"}},
    {"kinds": {"hologram": "Photos"}},
    {"kinds": {"document": "Documents"}},
    {"extensions": {"Pictures": ["jpg"]}},
    {"extensions": {"Photos": "jpg"}},
    {"extensions": {"Photos": ["../x"]}},
    {"extensions": {"Photos": ["jpg"], "Videos": ["jpg"]}},
    {"mime": {"image": "Photos"}},
    {"mime": {"image/": "Pics"}},
])
def test_invalid_mapping(bad) -> None:
    with pytest.raises(ValueError):
        validate_category_map(bad)
