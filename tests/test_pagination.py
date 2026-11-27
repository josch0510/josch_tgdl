import re
from datetime import date

import pytest

from tgdl.web.pagination import Pager, qs_with
from test_web import logged_in, with_password  # noqa: F401
from test_history_logs import add_download


def rows(html: str, section_id: str | None = None) -> int:
    if section_id:
        html = html.split(f'id="{section_id}"', 1)[1]
        html = html.split("</section>", 1)[0]
    body = html.split("<tbody>", 1)[1].split("</tbody>", 1)[0] if "<tbody>" in html else ""
    return body.count("<tr>")


# ------------------------------------------------------------- unit
def test_pager_math() -> None:
    p = Pager("x", page=3, per_page=10).set_total(55)
    assert (p.pages, p.offset, p.first_item, p.last_item) == (6, 20, 21, 30)
    assert Pager("x", page=99, per_page=25).set_total(55).page == 3   # clamped
    assert Pager("x", page=1, per_page=25).set_total(0).pages == 1
    assert Pager("x", page=2, per_page=10).slice(list(range(25))) == list(range(10, 20))


def test_pager_window() -> None:
    assert Pager("x", page=1, per_page=10).set_total(30).window() == [1, 2, 3]
    assert Pager("x", page=10, per_page=10).set_total(200).window() == [1, None, 8, 9, 10, 11, 12, None, 20]


def test_qs_with() -> None:
    assert qs_with({"q": "貓", "page": "2", "ok": "x"}, {"page": 3}, drop=("ok",)) == "q=%E8%B2%93&page=3"
    assert qs_with({"a": "1"}, {"a": None}) == ""


# -------------------------------------------------------------- web
async def test_history_per_page_choice_is_remembered(web, app, base_dir, with_password) -> None:  # noqa: F811
    for i in range(1, 31):
        await add_download(app, base_dir, i)
    await logged_in(web)
    assert rows((await web.get("/history")).text) == 25
    r = await web.get("/history", params={"per": 10})
    assert rows(r.text) == 10
    assert "tgdl_per_history=10" in r.headers.get("set-cookie", "")
    assert rows((await web.get("/history")).text) == 10              # remembered
    assert rows((await web.get("/history", params={"per": 50})).text) == 30
    assert rows((await web.get("/history", params={"per": 7})).text) == 30  # invalid -> cookie (50)
    html = (await web.get("/history", params={"per": 10, "page": 2, "q": "bin"})).text
    # page links keep the filter and the rows-per-page value
    assert 'href="/history?per=10&amp;page=3&amp;q=bin"' in html or "q=bin" in html and "page=3" in html
    assert re.search(r'<option value="10" selected>', html)


async def test_monitors_lists_paginate_independently(web, app, with_password) -> None:  # noqa: F811
    from tgdl.telegram.client import ChatInfo
    for i in range(1, 31):
        await app.monitors.add(ChatInfo(-1000 - i, f"Chat {i:02d}", "channel"))
    for i in range(12):
        await app.repo.create_backfill(-1001, *__import__("tgdl.telegram.backfill", fromlist=["x"])
                                       .local_range_to_utc(date(2026, 1, 1), date(2026, 1, 2), app.settings.tz))
    await logged_in(web)
    html = (await web.get("/monitors")).text
    first = html.split('id="backfill-history"')[0]
    assert first.count("/toggle") == 25
    assert rows(html, "backfill-history") == 12
    html = (await web.get("/monitors", params={"m_page": 2, "b_per": 10})).text
    first = html.split('id="backfill-history"')[0]
    assert first.count("/toggle") == 5 and "Chat 26" in first
    assert rows(html, "backfill-history") == 10
    # forms carry the current page so actions return to it
    assert 'name="back" value="/monitors?m_page=2&amp;b_per=10"' in html


async def test_toggle_returns_to_same_page(web, app, with_password) -> None:  # noqa: F811
    from tgdl.telegram.client import ChatInfo
    for i in range(1, 31):
        await app.monitors.add(ChatInfo(-1000 - i, f"Chat {i:02d}", "channel"))
    token = await logged_in(web)
    r = await web.post("/monitors/-1030/toggle", data={"csrf_token": token, "back": "/monitors?m_page=2"})
    assert r.headers["location"].startswith("/monitors?m_page=2&ok=monitor_disabled")
    r = await web.post("/monitors/-1030/toggle", data={"csrf_token": token, "back": "https://evil"})
    assert r.headers["location"].startswith("/monitors?ok=")


async def test_dialog_results_paginate_via_htmx(web, app, with_password) -> None:  # noqa: F811
    for i in range(1, 61):
        app.client.add_dialog(-2000 - i, f"Group {i:02d}")
    await logged_in(web)
    html = (await web.get("/monitors/dialogs")).text
    assert html.count("/monitors/add") == 25
    assert 'hx-get="/monitors/dialogs?page=2"' in html and 'hx-target="#dialog-results"' in html
    html = (await web.get("/monitors/dialogs", params={"q": "Group", "page": 3, "refresh": 1})).text
    assert html.count("/monitors/add") == 10
    assert "refresh" not in html.split("pager-bar", 1)[1]   # paging never forces a reload


async def test_dashboard_per_chat_paginates(web, app, base_dir, with_password) -> None:  # noqa: F811
    for i in range(1, 13):
        await add_download(app, base_dir, 1, chat=-500 - i, title=f"Chat {i}")
    await logged_in(web)
    html = (await web.get("/", params={"c_per": 10})).text
    assert 'data-live-url="/partials/dashboard?c_per=10"' in html
    part = (await web.get("/partials/dashboard", params={"c_per": 10, "c_page": 2})).text
    assert part.split("各對話統計" if "各對話統計" in part else "Per chat", 1)[1].count("<tr>") == 1 + 2


async def test_logs_paginate(web, app, with_password) -> None:  # noqa: F811
    import logging

    from tgdl.logging_setup import log_event, setup_logging
    setup_logging(app.cfg.logs_dir, console=False)
    for i in range(40):
        log_event(logging.getLogger("tgdl.t"), logging.INFO, f"act{i:02d}")
    await logged_in(web)
    html = (await web.get("/logs", params={"per": 10})).text
    assert html.count('class="log-line') == 10 and "act39" in html          # newest first
    assert 'data-live-url="/partials/logs?' in html and "per=10" in html
    html = (await web.get("/logs", params={"per": 10, "page": 2})).text
    assert "act29" in html and "act39" not in html
