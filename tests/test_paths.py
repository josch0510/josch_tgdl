from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tgdl.core.paths import (
    DEFAULT_PATH_TEMPLATE, PathContext, TemplateError, render_path, render_prefix,
    unique_path, validate_path_template,
)

TPE = ZoneInfo("Asia/Taipei")


def ctx(base: Path, **kw) -> PathContext:
    d = dict(base_dir=base, chat_name="My Channel", chat_id=-1001234, category="Photos",
             msg_id=42, filename="pic.jpg",
             message_date=datetime(2026, 1, 1, 17, 30, tzinfo=timezone.utc), tz=TPE)
    d.update(kw)
    return PathContext(**d)


def test_default_template_uses_local_date(tmp_path: Path) -> None:
    # 2026-01-01 17:30 UTC == 2026-01-02 01:30 in Taipei
    p = render_path(DEFAULT_PATH_TEMPLATE, ctx(tmp_path))
    assert p == tmp_path / "My Channel" / "Photos" / "2026" / "2026-01-02" / "42_pic.jpg"


def test_utc_timezone(tmp_path: Path) -> None:
    p = render_path(DEFAULT_PATH_TEMPLATE, ctx(tmp_path, tz=ZoneInfo("UTC")))
    assert p.parent.name == "2026-01-01"


def test_naive_datetime_treated_as_utc(tmp_path: Path) -> None:
    p = render_path(DEFAULT_PATH_TEMPLATE, ctx(tmp_path, message_date=datetime(2026, 1, 1, 17, 30)))
    assert p.parent.name == "2026-01-02"


def test_prefix_variables(tmp_path: Path) -> None:
    p = render_path(DEFAULT_PATH_TEMPLATE,
                    ctx(tmp_path, prefix_template="{chat_name}_{date}_{category}_{msg_id}-"))
    assert p.name == "My Channel_2026-01-02_Photos_42-42_pic.jpg"


def test_prefix_unknown_variable_rejected() -> None:
    with pytest.raises(TemplateError):
        render_prefix("{filename}", chat_name="a", local_date=datetime.now(), msg_id=1, category="c")


def test_malicious_values_cannot_escape(tmp_path: Path) -> None:
    p = render_path(DEFAULT_PATH_TEMPLATE, ctx(tmp_path, chat_name="../../etc",
                                                filename="../../.bashrc",
                                                prefix_template="{chat_name}/"))
    assert p.is_relative_to(tmp_path)
    assert ".." not in p.relative_to(tmp_path).parts
    assert len(p.relative_to(tmp_path).parts) == 5


def test_cjk_names(tmp_path: Path) -> None:
    p = render_path(DEFAULT_PATH_TEMPLATE, ctx(tmp_path, chat_name="台灣頻道", filename="照片 一.jpg"))
    assert p.parts[-5] == "台灣頻道"
    assert p.name == "42_照片 一.jpg"


def test_long_filename_segment_limited(tmp_path: Path) -> None:
    p = render_path(DEFAULT_PATH_TEMPLATE, ctx(tmp_path, filename="長" * 200 + ".mkv"))
    assert len(p.name.encode()) <= 255 and p.name.endswith(".mkv")


def test_custom_template(tmp_path: Path) -> None:
    t = "{base_dir}/{chat_id}/{YYYY}-{MM}/{DD}/{grouped_id}_{filename}"
    p = render_path(t, ctx(tmp_path, grouped_id=777))
    assert p == tmp_path / "-1001234" / "2026-01" / "02" / "777_pic.jpg"


@pytest.mark.parametrize("bad", [
    "{chat_name}/{filename}",                 # must start with base_dir
    "/tmp/{filename}",
    "{base_dir}",                             # no filename
    "{base_dir}/{chat_name}",                 # last segment lacks filename
    "{base_dir}/../{filename}",
    "{base_dir}/./{filename}",
    "{base_dir}//{filename}",
    "{base_dir}/{unknown}/{filename}",
    "{base_dir}/{filename!r}",
    "{base_dir}/{filename:>10}",
    "{base_dir}/{base_dir}/{filename}",
    "{base_dir}/{}/{filename}",
    "{base_dir}/{chat_name/{filename}",
    "{base_dir}\\x/{filename}",
])
def test_invalid_templates(bad: str) -> None:
    with pytest.raises(TemplateError):
        validate_path_template(bad)


def test_unique_path(tmp_path: Path) -> None:
    f = tmp_path / "a.jpg"
    assert unique_path(f) == f
    f.write_bytes(b"x")
    assert unique_path(f) == tmp_path / "a_1.jpg"
    (tmp_path / "a_1.jpg.part").write_bytes(b"")
    assert unique_path(f) == tmp_path / "a_2.jpg"
