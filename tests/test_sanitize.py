import pytest

from tgdl.core.sanitize import sanitize_component, sanitize_filename, truncate_utf8


@pytest.mark.parametrize("raw, expected", [
    ("normal name", "normal name"),
    ("a/b\\c", "a_b_c"),
    ('x:y*z?"<>|', "x_y_z_____"),
    ("..", "_"),
    (".", "_"),
    ("...", "_"),
    ("", "_"),
    ("   ", "_"),
    (".hidden", "hidden"),
    ("trailing. . ", "trailing"),
    ("tab\there\nnew", "tabherenew"),
    ("multi   space", "multi space"),
    ("evil‮gnp.exe", "evilgnp.exe"),
    ("貓咪頻道 🐱", "貓咪頻道 🐱"),
])
def test_sanitize_component(raw: str, expected: str) -> None:
    assert sanitize_component(raw) == expected


def test_component_never_contains_separator() -> None:
    for raw in ["../../etc/passwd", "/abs/path", "..\\..\\x"]:
        out = sanitize_component(raw)
        assert "/" not in out and "\\" not in out
        assert out not in (".", "..")


def test_nfc_normalization() -> None:
    decomposed = "Café"
    assert sanitize_component(decomposed) == "Café"


def test_truncate_utf8_does_not_split_cjk() -> None:
    s = "測" * 100  # 3 bytes each
    out = truncate_utf8(s, 10)
    assert out == "測" * 3
    assert len(out.encode()) <= 10


def test_filename_truncation_keeps_extension_and_byte_limit() -> None:
    name = "影片" * 200 + ".mp4"
    out = sanitize_filename(name)
    assert out.endswith(".mp4")
    assert len(out.encode("utf-8")) <= 255
    out.encode("utf-8").decode("utf-8")  # valid UTF-8


def test_component_byte_limit() -> None:
    out = sanitize_component("頻" * 300)
    assert len(out.encode()) <= 255


def test_filename_without_extension() -> None:
    assert sanitize_filename("README") == "README"
    assert sanitize_filename("x" * 300) == "x" * 255


def test_long_bogus_extension_not_treated_as_extension() -> None:
    name = "a" * 300 + "." + "b" * 40
    out = sanitize_filename(name)
    assert len(out.encode()) <= 255
