"""Monitoring private chats (people and bots)."""
from pathlib import Path

from telethon.tl.types import User

from fakes import make_message, wait_until
from test_web import logged_in, with_password  # noqa: F401
from tgdl.telegram.client import ChatInfo, DialogCache


def test_from_entity_user_and_bot() -> None:
    assert ChatInfo.from_entity(User(id=5, first_name="小明", username="ming")) == \
        ChatInfo(5, "小明", "user", "ming")
    assert ChatInfo.from_entity(User(id=6, first_name="FileBot", bot=True)).chat_type == "bot"


async def test_dialogs_include_private_chats_and_bots(app) -> None:
    app.client.add_dialog(-1001, "News", group=False)
    app.client.add_dialog(-1002, "Dev group", group=True)
    app.client.add_user_dialog(11, "王小明", username="ming_wang")
    app.client.add_user_dialog(12, "Files Bot", bot=True, username="files_bot")
    app.client.add_user_dialog(13, "Deleted Account", deleted=True)
    app.client.add_user_dialog(99, "Me", is_self=True)
    items = await app.dialogs.get()
    types = {c.chat_id: c.chat_type for c in items}
    assert types == {99: "saved", -1001: "channel", -1002: "group", 11: "user", 12: "bot"}
    assert 13 not in types                                   # deleted accounts are hidden


async def test_type_filter_and_username_search(app) -> None:
    app.client.add_dialog(-1001, "News")
    app.client.add_user_dialog(11, "王小明", username="ming_wang")
    app.client.add_user_dialog(12, "Files Bot", bot=True, username="files_bot")
    items = await app.dialogs.get()
    assert [c.chat_id for c in DialogCache.search(items, "", "user")] == [11]
    assert [c.chat_id for c in DialogCache.search(items, "", "bot")] == [12]
    assert [c.chat_id for c in DialogCache.search(items, "", "channel")] == [-1001]
    assert len(DialogCache.search(items, "", "nonsense")) == 3      # unknown type -> all
    assert [c.chat_id for c in DialogCache.search(items, "@ming")] == [11]
    assert [c.chat_id for c in DialogCache.search(items, "小明")] == [11]
    assert DialogCache.search(items, "files", "user") == []          # filter + query combined


async def test_private_chat_downloads_both_directions(app, base_dir) -> None:
    await app.monitors.add(ChatInfo(11, "王小明", "user"))
    incoming = app.client.add(11, make_message(1, name="from_friend.jpg", kind="document"))
    outgoing = app.client.add(11, make_message(2, name="sent_by_me.pdf"))
    j1 = await app.monitors.handle_message(11, incoming)   # event.chat_id == the other user's id
    j2 = await app.monitors.handle_message(11, outgoing)
    assert j1 and j2
    app.pool.start()
    await wait_until(lambda: _done(app, j1))
    await wait_until(lambda: _done(app, j2))
    rows = await app.repo.db.fetchall("SELECT saved_path FROM downloads ORDER BY message_id")
    assert all(Path(r[0]).relative_to(base_dir.resolve()).parts[0] == "王小明" for r in rows)


async def _done(app, jid):
    return (await app.repo.get_job(jid))["status"] == "done"


async def test_web_type_filter(web, app, with_password) -> None:  # noqa: F811
    app.client.add_dialog(-1001, "News")
    app.client.add_user_dialog(11, "王小明", username="ming_wang")
    app.client.add_user_dialog(12, "Files Bot", bot=True)
    token = await logged_in(web)
    await web.get("/lang/zh-TW")
    page = (await web.get("/monitors")).text
    assert 'name="type"' in page and "私人對話" in page and "機器人" in page
    html = (await web.get("/monitors/dialogs", params={"type": "user"})).text
    assert "王小明" in html and "@ming_wang" in html and "News" not in html and "Files Bot" not in html
    r = await web.post("/monitors/add", data={"csrf_token": token, "chat_id": "11"})
    assert "ok=monitor_added" in r.headers["location"]
    assert "私人對話" in (await web.get("/monitors")).text
