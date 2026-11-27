"""Download direction: all vs. received only."""
import re
from datetime import date, datetime, timezone

from fakes import make_message, wait_until
from test_web import logged_in, with_password  # noqa: F401
from tgdl.core.filters import ChatRules, filter_reason
from tgdl.core.media import extract_media
from tgdl.telegram.client import SAVED_MESSAGES_TITLE, ChatInfo


def msg(mid, out, **kw):
    m = make_message(mid, **kw)
    m.out = out
    return m


def test_filter_reason_direction(tmp_path) -> None:
    info = extract_media(make_message(1))
    rules = ChatRules(media_types=frozenset({"document"}), download_dir=tmp_path, direction="incoming")
    assert filter_reason(info, rules, outgoing=True) == "outgoing"
    assert filter_reason(info, rules, outgoing=False) is None
    all_rules = ChatRules(media_types=frozenset({"document"}), download_dir=tmp_path)
    assert filter_reason(info, all_rules, outgoing=True) is None


async def add(app, chat_id, title="Chat", chat_type="user", **overrides):
    await app.monitors.add(ChatInfo(chat_id, title, chat_type))
    if overrides:
        await app.repo.update_monitor(chat_id, **overrides)
        await app.monitors.reload(chat_id)


async def test_global_default_and_per_chat_override(app) -> None:
    await add(app, 1)                               # uses global default
    await add(app, 2, direction="incoming")         # override
    await add(app, 3, direction="all")              # override back to all
    assert await app.monitors.handle_message(1, msg(10, out=True))           # default: all
    assert await app.monitors.handle_message(2, msg(11, out=True)) is None   # sent -> skipped
    assert await app.monitors.handle_message(2, msg(12, out=False))          # received -> kept
    await app.settings.update({"default_direction": "incoming"})
    assert await app.monitors.handle_message(1, msg(13, out=True)) is None   # follows new default
    assert await app.monitors.handle_message(3, msg(14, out=True))           # override wins


async def test_saved_messages_ignore_direction(app) -> None:
    await app.settings.update({"default_direction": "incoming"})
    await add(app, 777, SAVED_MESSAGES_TITLE, "saved", direction="incoming")
    assert await app.monitors.handle_message(777, msg(1, out=True))          # still downloaded


async def test_backfill_respects_direction(app) -> None:
    await app.settings.update({"timezone": "UTC"})
    await add(app, 5, direction="incoming")
    d = datetime(2026, 1, 10, 12, tzinfo=timezone.utc)
    for i in range(1, 7):
        app.client.add(5, msg(i, out=i % 2 == 0, date=d))     # even ids sent by me
    task = await app.backfill.start(5, date(2026, 1, 10), date(2026, 1, 10))
    await wait_until(lambda: task not in app.backfill._tasks)
    rows = await app.repo.db.fetchall("SELECT message_id FROM jobs ORDER BY message_id")
    assert [r[0] for r in rows] == [1, 3, 5]


async def test_web_forms(web, app, with_password) -> None:  # noqa: F811
    await add(app, 11, "王小明")
    await add(app, 777, SAVED_MESSAGES_TITLE, "saved")
    token = await logged_in(web)
    await web.get("/lang/zh-TW")
    form = (await web.get("/monitors/11/settings")).text
    assert 'name="direction" value="incoming"' in form and "只下載收到的" in form
    assert 'name="direction"' not in (await web.get("/monitors/777/settings")).text  # saved: note only

    r = await web.post("/monitors/11/settings", data={"csrf_token": token, "use_default_types": "1",
                                                      "use_default_prefix": "1", "direction": "incoming"})
    assert r.status_code == 303 and app.monitors.monitors[11]["direction"] == "incoming"
    page = (await web.get("/monitors")).text
    assert re.search(r"只下載收到的\s*<span class=\"tag\">自訂</span>", page)
    r = await web.post("/monitors/11/settings", data={"csrf_token": token, "use_default_types": "1",
                                                      "use_default_prefix": "1", "direction": "sideways"})
    assert r.status_code == 400
    r = await web.post("/monitors/11/settings", data={"csrf_token": token, "use_default_types": "1",
                                                      "use_default_prefix": "1", "direction": ""})
    assert app.monitors.monitors[11]["direction"] is None                       # back to default

    s = app.settings.all()
    data = {"csrf_token": token, "default_dir": s["default_dir"], "concurrency": "3",
            "path_template": s["path_template"], "timezone": s["timezone"], "language": "zh-TW",
            "default_media_types": s["default_media_types"], "mime": "image/ = Photos",
            "resume_downloads": "1", "default_direction": "incoming"}
    data.update({f"kind_{k}": v for k, v in s["category_map"]["kinds"].items()})
    data.update({f"ext_{c}": ", ".join(e) for c, e in s["category_map"]["extensions"].items()})
    r = await web.post("/settings", data=data)
    assert r.status_code == 303 and app.settings.get("default_direction") == "incoming"
    r = await web.post("/settings", data={**data, "default_direction": "both"})
    assert r.status_code == 400
