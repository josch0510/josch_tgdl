import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from fakes import make_message
from test_web import hidden, logged_in, with_password  # noqa: F401 - fixtures
from tgdl.logreader import parse_entries, read_log, tail_lines
from tgdl.storage.repo import HistoryFilter, NewJob

NOW = datetime(2026, 3, 1, 12, tzinfo=timezone.utc)


async def add_download(app, base_dir: Path, msg_id: int, *, chat=-100, name="a.bin",
                       category="Others", exists=True, title="Chan") -> int:
    jid, _ = await app.repo.enqueue(NewJob(chat, title, msg_id, None, f"doc:{chat}:{msg_id}", "document",
                                           name, 10, NOW))
    path = base_dir / f"{chat}_{msg_id}.bin"
    if exists:
        path.write_bytes(b"x" * 10)
    await app.repo.record_download(await app.repo.get_job(jid), category=category, file_name=name,
                                   file_size=10, saved_path=str(path))
    row = await app.repo.db.fetchone("SELECT id FROM downloads WHERE chat_id=? AND message_id=?",
                                     (chat, msg_id))
    return row["id"]


async def add_failed(app, msg_id: int, chat=-100, name="f.bin") -> int:
    jid, _ = await app.repo.enqueue(NewJob(chat, "Chan", msg_id, None, f"doc:f{msg_id}", "document",
                                           name, 10, NOW))
    await app.repo.set_job_status(jid, "failed", error="ConnectionError")
    return jid


# ------------------------------------------------------------- repository
async def test_search_filters(app, base_dir) -> None:
    await add_download(app, base_dir, 1, name="holiday.jpg", category="Photos")
    await add_download(app, base_dir, 2, name="report_100%.pdf", category="Documents")
    await add_download(app, base_dir, 3, chat=-200, name="holiday.mp4", category="Videos")
    search = app.repo.search_downloads
    assert (await search(HistoryFilter(), limit=50, offset=0))[1] == 3
    assert (await search(HistoryFilter(query="HOLIDAY"), limit=50, offset=0))[1] == 2
    assert (await search(HistoryFilter(chat_id=-200), limit=50, offset=0))[1] == 1
    assert (await search(HistoryFilter(category="Documents"), limit=50, offset=0))[1] == 1
    # LIKE wildcards are matched literally
    assert (await search(HistoryFilter(query="100%"), limit=50, offset=0))[1] == 1
    assert (await search(HistoryFilter(query="%"), limit=50, offset=0))[1] == 1
    assert (await search(HistoryFilter(query="_"), limit=50, offset=0))[1] == 1
    future = datetime.now(timezone.utc) + timedelta(days=1)
    assert (await search(HistoryFilter(since=future), limit=50, offset=0))[1] == 0
    rows, total = await search(HistoryFilter(), limit=2, offset=2)
    assert total == 3 and len(rows) == 1


async def test_retry_failed_job(app) -> None:
    jid = await add_failed(app, 1)
    assert await app.repo.retry_job(jid) == "queued"
    job = await app.repo.get_job(jid)
    assert job["status"] == "pending" and job["attempts"] == 0 and job["source"] == "retry"
    assert await app.repo.retry_job(jid) == "not_failed"


async def test_retry_skips_already_downloaded(app, base_dir) -> None:
    jid = await add_failed(app, 1)
    # the same message got downloaded later by another job
    job = await app.repo.get_job(jid)
    await app.repo.db.execute("UPDATE jobs SET status='done' WHERE id=?", (jid,))
    await app.repo.record_download(job, category="Others", file_name="f", file_size=1, saved_path="/x")
    await app.repo.db.execute("UPDATE jobs SET status='failed' WHERE id=?", (jid,))
    assert await app.repo.retry_job(jid) == "skipped"


async def test_requeue_missing_download(app, base_dir) -> None:
    did = await add_download(app, base_dir, 7, exists=False)
    jid = await app.repo.requeue_download(did)
    assert jid and await app.repo.get_download(did) is None
    job = await app.repo.get_job(jid)
    assert job["status"] == "pending" and job["source"] == "redownload" and job["message_id"] == 7


async def test_redownload_end_to_end(app, base_dir) -> None:
    """Missing file -> requeue -> worker downloads it again -> new record."""
    msg = app.client.add(-100, make_message(9, name="lost.bin"))
    jid, _ = await app.queue.submit(chat_id=-100, chat_title="Chan", message=msg, source="live", monitor=None)
    app.pool.start()
    from fakes import wait_until
    await wait_until(lambda: _status(app, jid, "done"))
    d = (await app.repo.search_downloads(HistoryFilter(), limit=1, offset=0))[0][0]
    Path(d["saved_path"]).unlink()
    new_job = await app.repo.requeue_download(d["id"])
    await app.queue.notify()
    await wait_until(lambda: _status(app, new_job, "done"))
    assert Path((await app.repo.search_downloads(HistoryFilter(), limit=1, offset=0))[0][0]["saved_path"]).exists()


async def _status(app, jid, s):
    return (await app.repo.get_job(jid))["status"] == s


# --------------------------------------------------------------- web
async def test_history_page_and_actions(web, app, base_dir, with_password) -> None:  # noqa: F811
    await add_download(app, base_dir, 1, name="<script>alert(1)</script>.jpg", category="Photos")
    missing_id = await add_download(app, base_dir, 2, name="gone.pdf", exists=False)
    failed = await add_failed(app, 3)
    token = await logged_in(web)

    html = (await web.get("/history")).text
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html  # escaped
    assert html.count('action="/history/redownload/') == 1  # only the missing file

    r = await web.get("/history", params={"q": "gone"})
    assert "gone.pdf" in r.text and "alert(1)" not in r.text

    r = await web.get("/history", params={"tab": "failed"})
    assert "ConnectionError" in r.text

    r = await web.post(f"/history/redownload/{missing_id}", data={"csrf_token": token, "back": "/history?q=gone"})
    assert r.status_code == 303 and r.headers["location"].startswith("/history?q=gone")
    assert "ok=redownload_queued" in r.headers["location"]

    r = await web.post(f"/history/retry/{failed}", data={"csrf_token": token, "back": "https://evil.example"})
    assert r.headers["location"].startswith("/history?ok=job_requeued")

    r = await web.post("/history/retry-failed", data={"csrf_token": token})
    assert "n=0" in r.headers["location"]

    # CSRF required
    assert (await web.post(f"/history/retry/{failed}")).status_code == 403


async def test_redownload_refuses_existing_file(web, app, base_dir, with_password) -> None:  # noqa: F811
    did = await add_download(app, base_dir, 1)
    token = await logged_in(web)
    r = await web.post(f"/history/redownload/{did}", data={"csrf_token": token})
    assert "err=file_exists" in r.headers["location"]


async def test_redownload_missing_bulk(web, app, base_dir, with_password) -> None:  # noqa: F811
    await add_download(app, base_dir, 1)
    await add_download(app, base_dir, 2, exists=False)
    await add_download(app, base_dir, 3, exists=False)
    token = await logged_in(web)
    r = await web.post("/history/redownload-missing", data={"csrf_token": token})
    assert "ok=missing_requeued" in r.headers["location"] and "n=2" in r.headers["location"]
    page = await web.get(r.headers["location"])
    assert re.search(r"2", page.text)
    assert (await app.repo.job_counts())["pending"] == 2


async def test_history_pagination(web, app, base_dir, with_password) -> None:  # noqa: F811
    for i in range(1, 56):
        await add_download(app, base_dir, i, name=f"f{i}.bin")
    await logged_in(web)
    p1 = (await web.get("/history")).text
    assert "page=2" in p1
    p2 = (await web.get("/history", params={"page": 2})).text
    assert p2.count("<tr>") == 1 + 5


# --------------------------------------------------------------- logs
def test_tail_and_parse(tmp_path: Path) -> None:
    log = tmp_path / "t.log"
    lines = [f"2026-01-01 00:00:{i:02d},000 | {'ERROR' if i == 3 else 'INFO'} | chat=- | msg {i}"
             for i in range(10)]
    lines.insert(4, "Traceback (most recent call last):")
    lines.insert(5, "  ValueError: boom")
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert tail_lines(log, 3)[-1].endswith("msg 9") and len(tail_lines(log, 3)) == 3
    entries = parse_entries(tail_lines(log, 100))
    assert len(entries) == 10
    err = [e for e in entries if e.level == "ERROR"][0]
    assert "ValueError: boom" in err.text
    assert [e.level for e in read_log(log, lines=100, min_level="ERROR")] == ["ERROR"]
    assert len(read_log(log, lines=100, query="MSG 7")) == 1


def test_tail_large_file(tmp_path: Path) -> None:
    log = tmp_path / "big.log"
    with log.open("w") as fh:
        for i in range(200_000):
            fh.write(f"2026-01-01 00:00:00,000 | INFO | line {i}\n")
    out = tail_lines(log, 5)
    assert out == [f"2026-01-01 00:00:00,000 | INFO | line {i}" for i in range(199_995, 200_000)]


async def test_logs_page(web, app, with_password) -> None:  # noqa: F811
    from tgdl.logging_setup import setup_logging, log_event
    setup_logging(app.cfg.logs_dir, console=False)
    lg = logging.getLogger("tgdl.test")
    log_event(lg, logging.INFO, "info_action", "hello <b>world</b>")
    log_event(lg, logging.ERROR, "error_action", "something broke")
    for h in logging.getLogger("tgdl").handlers:
        h.flush()
    await logged_in(web)
    html = (await web.get("/logs", params={"level": "INFO"})).text
    assert "action=info_action" in html and "action=error_action" in html
    assert "hello &lt;b&gt;world&lt;/b&gt;" in html
    html = (await web.get("/logs", params={"level": "ERROR"})).text
    assert "info_action" not in html and "error_action" in html
    html = (await web.get("/logs", params={"source": "audit"})).text
    assert "action=login_success" in html
    assert app.cfg.api_hash not in (await web.get("/logs", params={"level": "DEBUG", "lines": 5000})).text
    # invalid parameters fall back to safe defaults
    assert (await web.get("/logs", params={"source": "../../etc/passwd", "lines": "99999"})).status_code == 200


async def test_log_events_forwarded(app) -> None:
    from tgdl.logging_setup import log_event, ring_buffer
    detach = app.forward_logs()
    try:
        async with app.bus.subscribe() as q:
            log_event(logging.getLogger("tgdl.test"), logging.WARNING, "x", "y")
            ev = await asyncio.wait_for(q.get(), 1)
            assert ev.type == "log" and ev.data["level"] == "WARNING"
    finally:
        detach()
    assert not ring_buffer.listeners
