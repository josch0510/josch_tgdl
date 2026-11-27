import re

from test_web import logged_in, with_password  # noqa: F401
from tgdl.telegram.client import ChatInfo
from tgdl.web.monitor_view import describe


async def add(app, chat_id, title, **overrides):
    await app.monitors.add(ChatInfo(chat_id, title, "channel"))
    if overrides:
        await app.repo.update_monitor(chat_id, **overrides)
        await app.monitors.reload(chat_id)
    return app.monitors.monitors[chat_id]


async def test_describe_defaults_and_overrides(app, base_dir) -> None:
    m = await add(app, -1, "Defaults")
    v = describe(app, m)
    assert not (v.types_custom or v.dir_custom or v.prefix_custom or v.has_filters)
    assert v.media_types == list(app.settings.get("default_media_types")) or \
        set(v.media_types) == set(app.settings.get("default_media_types"))
    assert v.download_dir == app.settings.get("default_dir")

    m = await add(app, -2, "Custom", media_types=["video", "photo"], download_dir=str(base_dir / "v"),
                  filename_prefix="{date}_", min_size=1024, keyword="4K")
    v = describe(app, m)
    assert v.media_types == ["photo", "video"]          # canonical order
    assert v.types_custom and v.dir_custom and v.prefix_custom and v.has_filters
    assert v.download_dir == str(base_dir.resolve() / "v") and v.prefix == "{date}_"


async def test_describe_dir_no_longer_allowed(app, tmp_path) -> None:
    m = await add(app, -3, "Moved", download_dir=str(tmp_path / "outside"))
    v = describe(app, m)
    assert v.dir_error == "dir.outside_allowed" and v.download_dir.endswith("outside")


async def test_dashboard_shows_only_counts(web, app, with_password) -> None:  # noqa: F811
    await add(app, -1, "貓咪頻道")
    await add(app, -2, "Disabled chat", enabled=False)
    await logged_in(web)
    html = (await web.get("/")).text
    assert "1 / 2" in html                                   # enabled / total, plain text
    assert 'id="monitored"' not in html and 'href="#monitored"' not in html
    assert "貓咪頻道" not in html and "settings-summary" not in html


async def test_monitors_page_shows_effective_values(web, app, base_dir, with_password) -> None:  # noqa: F811
    await add(app, -1, "Chan", download_dir=str(base_dir / "chan"))
    await logged_in(web)
    html = (await web.get("/monitors")).text
    assert str(base_dir.resolve() / "chan") in html and 'class="settings-summary"' in html
    # edit is a button, not a plain link
    assert re.search(r'<a class="btn btn-small edit-btn" href="/monitors/-1/settings" role="button">', html)


async def test_custom_type_checkboxes_prefilled_with_defaults(web, app, with_password) -> None:  # noqa: F811
    import re
    await add(app, -1, "Chan")
    await logged_in(web)
    html = (await web.get("/monitors/-1/settings")).text
    checked = set(re.findall(r'name="media_types" value="(\w+)" checked', html))
    assert checked == set(app.settings.get("default_media_types"))
