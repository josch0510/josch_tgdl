import asyncio
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from telethon.errors import FloodWaitError

from fakes import make_message, wait_until
from tgdl.telegram.backfill import BackfillError, local_range_to_utc
from tgdl.telegram.client import ChatInfo


async def add_monitor(app, chat_id=-100, title="Chan", enabled=True):
    await app.monitors.add(ChatInfo(chat_id, title, "channel"))
    if not enabled:
        await app.monitors.set_enabled(chat_id, False)


async def test_only_enabled_monitors_enqueue(app) -> None:
    await add_monitor(app, -100)
    await add_monitor(app, -200, enabled=False)
    assert await app.monitors.handle_message(-100, make_message(1)) is not None
    assert await app.monitors.handle_message(-200, make_message(2)) is None
    assert await app.monitors.handle_message(-300, make_message(3)) is None  # not monitored
    await app.monitors.set_enabled(-200, True)
    assert await app.monitors.handle_message(-200, make_message(4)) is not None


async def test_multiple_chats_simultaneously(app) -> None:
    for c in (-1, -2, -3):
        await add_monitor(app, c, f"chat{c}")
    results = await asyncio.gather(*(app.monitors.handle_message(c, make_message(10, media_id=abs(c)))
                                     for c in (-1, -2, -3)))
    assert all(results)
    assert (await app.repo.job_counts())["pending"] == 3


async def test_message_without_media_ignored(app) -> None:
    await add_monitor(app)
    m = make_message(1)
    m.media = None
    assert await app.monitors.handle_message(-100, m) is None


async def test_per_chat_filters_applied(app) -> None:
    await add_monitor(app)
    await app.repo.update_monitor(-100, media_types=["photo"])
    await app.monitors.reload(-100)
    assert await app.monitors.handle_message(-100, make_message(1)) is None
    assert await app.monitors.handle_message(-100, make_message(2, kind="photo")) is not None


async def test_album_sweep_catches_missing_items(app) -> None:
    await add_monitor(app)
    items = [app.client.add(-100, make_message(i, kind="photo", grouped_id=77)) for i in (20, 21, 22)]
    app.client.add(-100, make_message(23, kind="photo"))  # not part of the album
    await app.monitors.handle_message(-100, items[0])  # only first update arrives
    await wait_until(lambda: not app.monitors._album_tasks)
    rows = await app.repo.db.fetchall("SELECT message_id FROM jobs ORDER BY message_id")
    assert [r[0] for r in rows] == [20, 21, 22]


def test_local_range_to_utc() -> None:
    start, end = local_range_to_utc(date(2026, 1, 1), date(2026, 1, 31), ZoneInfo("Asia/Taipei"))
    assert start == datetime(2025, 12, 31, 16, tzinfo=timezone.utc)
    assert end == datetime(2026, 1, 31, 16, tzinfo=timezone.utc)
    with pytest.raises(BackfillError):
        local_range_to_utc(date(2026, 2, 1), date(2026, 1, 1), ZoneInfo("UTC"))


async def test_backfill_enqueues_range_and_survives_flood(app) -> None:
    await app.settings.update({"timezone": "UTC"})
    await add_monitor(app)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(1, 31):  # one message per day, Jan 1..30
        app.client.add(-100, make_message(i, date=base + timedelta(days=i - 1, hours=12)))
    app.client.iter_failures[15] = [FloodWaitError(request=None, capture=0)]
    task_id = await app.backfill.start(-100, date(2026, 1, 10), date(2026, 1, 20))
    await wait_until(lambda: task_id not in app.backfill._tasks)
    rows = await app.repo.db.fetchall("SELECT message_id, source, priority FROM jobs ORDER BY message_id")
    assert [r[0] for r in rows] == list(range(10, 21))
    assert all(r[1] == "backfill" and r[2] == 10 for r in rows)
    bf = (await app.repo.list_backfills())[0]
    assert bf["status"] == "done" and bf["enqueued"] == 11
    assert app.api_gate.total_triggers == 1


async def test_backfill_requires_monitor_and_one_at_a_time(app) -> None:
    with pytest.raises(BackfillError):
        await app.backfill.start(-999, date(2026, 1, 1), date(2026, 1, 2))
    await add_monitor(app)
    app.client.add(-100, make_message(1, date=datetime(2026, 1, 1, tzinfo=timezone.utc)))
    app.api_gate.trigger(0.3)  # keep the first one busy
    await app.backfill.start(-100, date(2026, 1, 1), date(2026, 1, 2))
    with pytest.raises(BackfillError) as e:
        await app.backfill.start(-100, date(2026, 1, 1), date(2026, 1, 2))
    assert e.value.code == "backfill.already_running"


async def test_backfill_cancel(app) -> None:
    await add_monitor(app)
    app.api_gate.trigger(5)
    tid = await app.backfill.start(-100, date(2026, 1, 1), date(2026, 1, 2))
    assert await app.backfill.cancel(tid)
    assert (await app.repo.list_backfills())[0]["status"] == "cancelled"
