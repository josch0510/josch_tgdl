import asyncio
from pathlib import Path

from telethon.errors import FloodWaitError, ServerError

from fakes import make_message, wait_until
from tgdl.core.paths import PathNotAllowed
from tgdl.downloader.worker import backoff_delay, is_transient
from tgdl.downloader.download import SizeMismatch
from tgdl.downloader.pipeline import MediaGone


async def submit(app, chat_id, msg, source="live"):
    app.client.add(chat_id, msg)
    return await app.queue.submit(chat_id=chat_id, chat_title="Chan", message=msg,
                                  source=source, monitor=None)


async def status(app, jid):
    return (await app.repo.get_job(jid))["status"]


def test_error_classification() -> None:
    assert is_transient(ConnectionError())
    assert is_transient(SizeMismatch(1, 2))
    assert is_transient(ServerError(request=None, message="x", code=500))
    assert not is_transient(MediaGone())
    assert not is_transient(PathNotAllowed("dir.outside_allowed"))
    assert not is_transient(ValueError())
    assert [backoff_delay(i) for i in (1, 2, 3)] == [5.0, 10.0, 20.0]


async def test_downloads_respect_concurrency(app, base_dir: Path) -> None:
    await app.settings.update({"concurrency": 2})
    app.client.gate.clear()
    jids = [(await submit(app, -1, make_message(i)))[0] for i in range(1, 7)]
    app.pool.start()
    await wait_until(lambda: app.client.concurrent == 2)
    await asyncio.sleep(0.1)
    assert app.client.concurrent == 2 and len(app.pool.active) == 2
    app.client.gate.set()
    for jid in jids:
        await wait_until(lambda j=jid: _done(app, j))
    assert app.client.max_concurrent == 2
    assert len(list(base_dir.rglob("*.bin"))) == 6
    assert not list(base_dir.rglob("*.part"))


async def _done(app, jid):
    return await status(app, jid) == "done"


async def test_resize_at_runtime(app) -> None:
    await app.settings.update({"concurrency": 1})
    app.client.gate.clear()
    for i in range(1, 6):
        await submit(app, -1, make_message(i))
    app.pool.start()
    await wait_until(lambda: app.client.concurrent == 1)
    await app.settings.update({"concurrency": 4})
    await wait_until(lambda: app.client.concurrent == 4)
    await app.settings.update({"concurrency": 2})
    app.client.gate.set()
    await wait_until(lambda: app.pool.status()["workers"] == 2 and len(app.pool._tasks) == 2)


async def test_transient_error_retries_then_succeeds(app) -> None:
    msg = make_message(1)
    app.client.download_failures[1] = [ConnectionError("reset"), ConnectionError("reset")]
    jid, _ = await submit(app, -1, msg)
    app.pool.start()
    await wait_until(lambda: _done(app, jid))
    job = await app.repo.get_job(jid)
    assert job["attempts"] == 3


async def test_transient_error_fails_after_max_retries(app) -> None:
    app.client.download_failures[1] = [ConnectionError("x")] * 10
    jid, _ = await submit(app, -1, make_message(1))
    app.pool.start()
    await wait_until(lambda: _status_is(app, jid, "failed"))
    job = await app.repo.get_job(jid)
    assert job["attempts"] == 4 and job["last_error"] == "ConnectionError"  # 1 try + 3 retries


async def _status_is(app, jid, s):
    return await status(app, jid) == s


async def test_permanent_error_fails_immediately(app) -> None:
    msg = make_message(1)
    jid, _ = await submit(app, -1, msg)
    del app.client.messages[(-1, 1)]  # message deleted before download
    app.pool.start()
    await wait_until(lambda: _status_is(app, jid, "failed"))
    job = await app.repo.get_job(jid)
    assert job["attempts"] == 1 and job["last_error"] == "MediaGone"


async def test_flood_wait_pauses_and_requeues(app) -> None:
    await app.settings.update({"concurrency": 1})
    app.client.download_failures[1] = [FloodWaitError(request=None, capture=0)]
    app.download_gate.trigger  # noqa: B018
    jid, _ = await submit(app, -1, make_message(1))
    events = []
    async with app.bus.subscribe() as q:
        app.pool.start()
        await wait_until(lambda: _done(app, jid))
        while not q.empty():
            events.append(q.get_nowait())
    floods = [e for e in events if e.type == "flood"]
    assert floods and floods[0].data["scope"] == "download"
    assert app.download_gate.total_triggers == 1
    # FloodWait is not the job's fault: the attempt is not counted
    assert (await app.repo.get_job(jid))["attempts"] == 1


async def test_flood_gate_blocks_new_claims(app) -> None:
    app.download_gate.trigger(0.4)
    jid, _ = await submit(app, -1, make_message(1))
    app.pool.start()
    await asyncio.sleep(0.2)
    assert await status(app, jid) == "pending"
    assert app.pool.status()["flood"]["active"]
    await wait_until(lambda: _done(app, jid))


async def test_graceful_stop_requeues_and_removes_part(app, base_dir: Path) -> None:
    app.client.gate.clear()
    jid, _ = await submit(app, -1, make_message(1))
    app.pool.start()
    await wait_until(lambda: app.client.concurrent == 1)
    assert list(base_dir.rglob("*.part"))
    assert (await app.repo.get_job(jid))["part_path"]
    await app.pool.stop()
    assert not list(base_dir.rglob("*.part"))
    job = await app.repo.get_job(jid)
    assert job["status"] == "pending" and job["attempts"] == 0


async def test_live_jobs_before_backfill(app) -> None:
    await app.settings.update({"concurrency": 1})
    b, _ = await submit(app, -1, make_message(1), source="backfill")
    live, _ = await submit(app, -1, make_message(2), source="live")
    first = await app.repo.claim_job()
    assert first["id"] == live


async def test_queue_dedup_same_file_twice(app) -> None:
    jid, _ = await submit(app, -1, make_message(1, media_id=500))
    again = await submit(app, -2, make_message(9, media_id=500))
    assert jid and again == (None, "queued_media")


async def test_shutdown_rejects_new_jobs(app) -> None:
    app.queue.accepting = False
    assert await submit(app, -1, make_message(1)) == (None, "shutting_down")
