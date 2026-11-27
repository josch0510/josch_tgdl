import os
from pathlib import Path

import pytest

from tgdl.core.paths import PathNotAllowed, resolve_allowed


def test_inside_allowed(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    assert resolve_allowed(root, [root]) == root.resolve()
    assert resolve_allowed(root / "a" / "b", [root]) == root.resolve() / "a" / "b"


@pytest.mark.parametrize("rel", ["../outside", "a/../../outside", "a/../../../etc"])
def test_traversal_rejected(tmp_path: Path, rel: str) -> None:
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(PathNotAllowed):
        resolve_allowed(str(root) + "/" + rel, [root])


def test_traversal_that_stays_inside_is_ok(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    assert resolve_allowed(f"{root}/a/../b", [root]) == root.resolve() / "b"


def test_prefix_confusion_rejected(tmp_path: Path) -> None:
    root = tmp_path / "media"
    root.mkdir()
    (tmp_path / "media2").mkdir()
    with pytest.raises(PathNotAllowed):
        resolve_allowed(tmp_path / "media2", [root])


def test_relative_rejected(tmp_path: Path) -> None:
    with pytest.raises(PathNotAllowed) as e:
        resolve_allowed("relative/dir", [tmp_path])
    assert e.value.code == "dir.not_absolute"


@pytest.mark.parametrize("bad", ["", "/tmp/\x00evil"])
def test_invalid_rejected(tmp_path: Path, bad: str) -> None:
    with pytest.raises(PathNotAllowed):
        resolve_allowed(bad, [tmp_path])


def test_symlink_escape_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, root / "link")
    with pytest.raises(PathNotAllowed):
        resolve_allowed(root / "link", [root])
    with pytest.raises(PathNotAllowed):
        resolve_allowed(root / "link" / "file.jpg", [root])


def test_symlinked_root_allowed(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    os.symlink(real, tmp_path / "alias")
    assert resolve_allowed(tmp_path / "alias" / "x", [real]) == real.resolve() / "x"


def test_multiple_roots(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    assert resolve_allowed(b / "x", [a, b]) == b.resolve() / "x"
    with pytest.raises(PathNotAllowed):
        resolve_allowed("/etc", [a, b])
