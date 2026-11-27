import asyncio
import os
from pathlib import Path

import pytest

from fakes import FakeClient, make_message
from tgdl.core.media import extract_media
from tgdl.downloader.download import ACTIVE_PARTS, SizeMismatch, download_to_file
from tgdl.downloader.pipeline import Pipeline


def test_extract_media_kinds() -> None:
    assert extract_media(make_message(1, kind="photo", media_id=5)).unique_id == "photo:5"
    assert extract_media(make_message(1, kind="voice")).kind == "voice"
    assert extract_media(make_message(1, kind="animation")).kind == "animation"
    info = extract_media(make_message(1, kind="video", name=None, mime="video/mp4"))
    assert info.kind == "video" and info.file_name.endswith(".mp4")
    m = make_message(1)
    m.web_preview = object()
    assert extract_media(m) is None


async def test_download_writes_part_then_renames(tmp_path: Path) -> None:
    client = FakeClient()
    final, size = await download_to_file(client, make_message(1), tmp_path / "d" / "a.bin",
                                         expected_size=1000)
    assert final == tmp_path / "d" / "a.bin" and size == 1000
    assert not list(tmp_path.rglob("*.part")) and not ACTIVE_PARTS


async def test_size_mismatch_removes_part(tmp_path: Path) -> None:
    with pytest.raises(SizeMismatch):
        await download_to_file(FakeClient(), make_message(1), tmp_path / "a.bin", expected_size=5)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("exc", [ConnectionError("x"), asyncio.CancelledError()])
async def test_error_or_cancel_removes_part(tmp_path: Path, exc: BaseException) -> None:
    with pytest.raises(type(exc)):
        await download_to_file(FakeClient(fail_with=exc), make_message(1), tmp_path / "a.bin",
                               expected_size=1000)
    assert not list(tmp_path.iterdir()) and not ACTIVE_PARTS


async def test_existing_file_not_overwritten(tmp_path: Path) -> None:
    (tmp_path / "a.bin").write_bytes(b"old")
    final, _ = await download_to_file(FakeClient(), make_message(1), tmp_path / "a.bin",
                                      expected_size=1000)
    assert final.name == "a_1.bin" and (tmp_path / "a.bin").read_bytes() == b"old"


async def test_pipeline_end_to_end(cfg, repo, settings, base_dir: Path) -> None:
    pipe = Pipeline(cfg, repo, settings)
    client = FakeClient()
    msg = make_message(7, name="報告.pdf", mime="application/pdf")
    jid, reason = await pipe.consider(chat_id=-1001, chat_title="測試頻道", message=msg,
                                      source="manual", monitor=None)
    assert jid and reason is None
    path = await pipe.execute(client, await repo.get_job(jid), msg)
    assert path == base_dir / "測試頻道" / "Documents" / "2026" / "2026-03-01" / "7_報告.pdf"
    assert path.read_bytes() == client.payload
    assert (await repo.get_job(jid))["status"] == "done"
    # same message again -> skipped, same media from another chat -> skipped
    assert (await pipe.consider(chat_id=-1001, chat_title="x", message=msg, source="live",
                                monitor=None)) == (None, "downloaded_message")
    other = make_message(99, media_id=70)
    assert (await pipe.consider(chat_id=-2002, chat_title="y", message=other, source="live",
                                monitor=None)) == (None, "downloaded_media")


async def test_pipeline_filters(cfg, repo, settings) -> None:
    pipe = Pipeline(cfg, repo, settings)
    mon = {"media_types": ["photo"], "min_size": None, "max_size": None, "keyword": None}
    r = await pipe.consider(chat_id=1, chat_title="c", message=make_message(1), source="live", monitor=mon)
    assert r == (None, "type_disabled")
    mon = {"media_types": None, "min_size": 5000}
    r = await pipe.consider(chat_id=1, chat_title="c", message=make_message(2), source="live", monitor=mon)
    assert r == (None, "too_small")
    mon = {"max_size": 10}
    r = await pipe.consider(chat_id=1, chat_title="c", message=make_message(3), source="live", monitor=mon)
    assert r == (None, "too_large")
    mon = {"keyword": "REPORT"}
    r = await pipe.consider(chat_id=1, chat_title="c", message=make_message(4, name="my_report.pdf"),
                            source="live", monitor=mon)
    assert r[0] is not None
    r = await pipe.consider(chat_id=1, chat_title="c", message=make_message(5, name="other.pdf"),
                            source="live", monitor=mon)
    assert r == (None, "keyword_mismatch")


async def test_album_items_each_downloaded(cfg, repo, settings, base_dir: Path) -> None:
    pipe = Pipeline(cfg, repo, settings)
    client = FakeClient()
    paths = []
    for mid in (10, 11, 12):
        m = make_message(mid, kind="photo", media_id=mid, grouped_id=555)
        jid, _ = await pipe.consider(chat_id=-1, chat_title="Album", message=m, source="live", monitor=None)
        paths.append(await pipe.execute(client, await repo.get_job(jid), m))
    assert len({p.name for p in paths}) == 3
    assert all(p.parent == paths[0].parent for p in paths)


async def test_monitor_dir_outside_allowed_rejected(cfg, repo, settings, tmp_path: Path) -> None:
    from tgdl.core.paths import PathNotAllowed
    pipe = Pipeline(cfg, repo, settings)
    with pytest.raises(PathNotAllowed):
        await pipe.consider(chat_id=1, chat_title="c", message=make_message(1), source="live",
                            monitor={"download_dir": str(tmp_path / "elsewhere")})


async def test_settings_validation(settings, base_dir: Path) -> None:
    from tgdl.settings import SettingsError
    with pytest.raises(SettingsError):
        await settings.update({"concurrency": 11})
    with pytest.raises(SettingsError):
        await settings.update({"timezone": "Mars/Base"})
    with pytest.raises(SettingsError):
        await settings.update({"default_dir": "/etc"})
    seen = {}
    settings.on_change(lambda diff: seen.update(diff))
    diff = await settings.update({"concurrency": 5, "default_dir": str(base_dir / "sub")})
    assert diff["concurrency"] == (3, 5) and "concurrency" in seen
    assert settings.get("default_dir") == str(base_dir.resolve() / "sub")
    # persisted
    from tgdl.settings import SettingsService
    s2 = SettingsService(settings.cfg, settings.repo)
    await s2.load()
    assert s2.get("concurrency") == 5
