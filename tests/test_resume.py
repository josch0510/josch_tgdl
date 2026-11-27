"""Resuming interrupted downloads (setting: resume_downloads)."""
import asyncio
from pathlib import Path

import pytest

from fakes import make_message, wait_until
from tgdl.downloader import download

PAYLOAD = bytes(range(256)) * 4 + b"tail"  # 1028 bytes, position-dependent content


@pytest.fixture(autouse=True)
def small_align(monkeypatch):
    monkeypatch.setattr(download, "RESUME_ALIGN", 256)


async def submit(app, msg_id=1, **kw):
    msg = app.client.add(-1, make_message(msg_id, size=len(PAYLOAD), **kw))
    jid, _ = await app.queue.submit(chat_id=-1, chat_title="Chan", message=msg, source="live",
                                    monitor=None)
    return jid


async def is_status(app, jid, status):
    return (await app.repo.get_job(jid))["status"] == status


async def interrupt_halfway(app, jid) -> Path:
    """Start the pool, stop it while the file is half written."""
    app.client.gate.clear()
    app.pool.start()
    await wait_until(lambda: app.client.concurrent == 1)
    await app.pool.stop()
    job = await app.repo.get_job(jid)
    assert job["status"] == "pending"
    return Path(job["part_path"])


def restart_pool(app):
    app.pool._stopping = False
    app.client.gate.set()
    app.pool.start()


async def test_stop_keeps_part_and_resumes(app, base_dir) -> None:
    app.client.payload = PAYLOAD
    jid = await submit(app)
    part = await interrupt_halfway(app, jid)
    assert part.exists() and part.stat().st_size == len(PAYLOAD) // 2  # 514 bytes kept

    restart_pool(app)
    await wait_until(lambda: is_status(app, jid, "done"))
    assert app.client.resume_offsets == [512]  # 514 aligned down to 256
    final = Path((await app.repo.db.fetchone("SELECT saved_path FROM downloads"))[0])
    assert final.read_bytes() == PAYLOAD and final.with_name(final.name + ".part") == part
    assert not part.exists() and (await app.repo.get_job(jid))["part_path"] is None


async def test_transient_error_resumes_on_retry(app) -> None:
    app.client.payload = PAYLOAD
    jid = await submit(app)
    app.client.fail_after_half[1] = [ConnectionError("reset")]
    app.pool.start()
    await wait_until(lambda: is_status(app, jid, "done"))
    assert app.client.resume_offsets == [512]
    final = Path((await app.repo.db.fetchone("SELECT saved_path FROM downloads"))[0])
    assert final.read_bytes() == PAYLOAD


async def test_error_while_resuming_resumes_again(app) -> None:
    app.client.payload = PAYLOAD
    jid = await submit(app)
    app.client.fail_after_half[1] = [ConnectionError("1"), ConnectionError("2")]
    app.pool.start()
    await wait_until(lambda: is_status(app, jid, "done"))
    assert app.client.resume_offsets == [512, 768]
    final = Path((await app.repo.db.fetchone("SELECT saved_path FROM downloads"))[0])
    assert final.read_bytes() == PAYLOAD


async def test_resume_disabled_restarts_from_zero(app) -> None:
    app.client.payload = PAYLOAD
    jid = await submit(app)
    part = await interrupt_halfway(app, jid)   # resume on: part kept
    await app.settings.update({"resume_downloads": False})
    restart_pool(app)
    await wait_until(lambda: is_status(app, jid, "done"))
    assert app.client.resume_offsets == []      # full download, old part discarded
    assert not part.exists()


async def test_oversized_part_is_discarded(app) -> None:
    app.client.payload = PAYLOAD
    jid = await submit(app)
    part = await interrupt_halfway(app, jid)
    part.write_bytes(b"x" * (len(PAYLOAD) + 10))
    restart_pool(app)
    await wait_until(lambda: is_status(app, jid, "done"))
    assert app.client.resume_offsets == []


async def test_changed_media_is_not_resumed(app) -> None:
    app.client.payload = PAYLOAD
    jid = await submit(app)
    part = await interrupt_halfway(app, jid)
    # the message was edited and now carries a different file
    app.client.add(-1, make_message(1, media_id=999, size=len(PAYLOAD)))
    restart_pool(app)
    await wait_until(lambda: is_status(app, jid, "done"))
    assert app.client.resume_offsets == [] and not part.exists()


async def test_part_outside_allowed_dirs_is_ignored(app, tmp_path) -> None:
    app.client.payload = PAYLOAD
    jid = await submit(app)
    outside = tmp_path / "elsewhere.part"
    outside.write_bytes(b"x" * 600)
    await app.repo.set_part_path(jid, str(outside))
    app.pool.start()
    await wait_until(lambda: is_status(app, jid, "done"))
    assert outside.exists() and app.client.resume_offsets == []  # never touched


async def test_size_mismatch_deletes_part(app, base_dir) -> None:
    app.client.payload = PAYLOAD
    msg = app.client.add(-1, make_message(1, size=len(PAYLOAD) + 1))  # Telegram size disagrees
    jid, _ = await app.queue.submit(chat_id=-1, chat_title="Chan", message=msg, source="live", monitor=None)
    app.pool.start()
    await wait_until(lambda: is_status(app, jid, "failed"))
    assert not list(base_dir.rglob("*.part"))


async def test_permanent_failure_deletes_kept_part(app, base_dir) -> None:
    app.client.payload = PAYLOAD
    jid = await submit(app)
    await interrupt_halfway(app, jid)
    del app.client.messages[(-1, 1)]  # message deleted meanwhile
    restart_pool(app)
    await wait_until(lambda: is_status(app, jid, "failed"))
    assert not list(base_dir.rglob("*.part"))
    assert (await app.repo.get_job(jid))["part_path"] is None


async def test_flood_wait_keeps_part(app) -> None:
    from telethon.errors import FloodWaitError
    app.client.payload = PAYLOAD
    jid = await submit(app)
    app.client.fail_after_half[1] = [FloodWaitError(request=None, capture=0)]
    app.pool.start()
    await wait_until(lambda: is_status(app, jid, "done"))
    assert app.client.resume_offsets == [512]
    assert (await app.repo.get_job(jid))["attempts"] == 1


async def test_crash_recovery_keeps_or_deletes(app, base_dir) -> None:
    from tgdl.app import remove_stale_parts
    app.client.payload = PAYLOAD
    jid = await submit(app)
    part = await interrupt_halfway(app, jid)
    # simulate a crash: job left "running" with its part
    await app.repo.db.execute("UPDATE jobs SET status='running' WHERE id=?", (jid,))
    assert await app.repo.reset_running_jobs(keep_parts=True) == []
    job = await app.repo.get_job(jid)
    assert job["status"] == "pending" and job["part_path"] == str(part) and part.exists()
    # later started with resume off: everything is cleaned
    stale = await app.repo.detach_all_parts()
    assert await remove_stale_parts(app.cfg, stale) == 1 and not part.exists()


async def test_settings_page_toggle(web, app, base_dir) -> None:
    from test_web import logged_in
    from tgdl.passwords import hash_password
    await app.repo.set_password_hash(hash_password("correct horse battery"))
    token = await logged_in(web)
    html = (await web.get("/settings")).text
    assert 'name="resume_downloads" value="1" checked' in html
    s = app.settings.all()
    data = {"csrf_token": token, "default_dir": s["default_dir"], "concurrency": "3",
            "path_template": s["path_template"], "timezone": s["timezone"],
            "default_media_types": s["default_media_types"], "language": "en", "mime": "image/ = Photos"}
    data.update({f"kind_{k}": v for k, v in s["category_map"]["kinds"].items()})
    data.update({f"ext_{c}": ", ".join(e) for c, e in s["category_map"]["extensions"].items()})
    r = await web.post("/settings", data=data)  # checkbox absent -> off
    assert r.status_code == 303 and app.settings.get("resume_downloads") is False
    r = await web.post("/settings", data={**data, "resume_downloads": "1"})
    assert app.settings.get("resume_downloads") is True
