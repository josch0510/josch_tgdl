"""Monitoring "Saved Messages" (我的收藏): the private chat with yourself."""
from pathlib import Path

from telethon.tl.types import User

from fakes import make_message, wait_until
from test_web import logged_in, with_password  # noqa: F401
from tgdl.telegram.client import SAVED_MESSAGES_TITLE, ChatInfo, DialogCache

ME = 777000111


def test_from_entity_self_user() -> None:
    info = ChatInfo.from_entity(User(id=ME, is_self=True, first_name="Josch"))
    assert info == ChatInfo(ME, SAVED_MESSAGES_TITLE, "saved")
    other = ChatInfo.from_entity(User(id=5, first_name="Friend"))
    assert other.chat_type == "user" and other.title == "Friend"


async def test_dialog_list_includes_saved_messages_first(app) -> None:
    app.client.add_dialog(-1001, "Alpha channel")
    app.client.add_user_dialog(ME, "Josch", is_self=True)
    app.client.add_user_dialog(42, "A friend")            # other private chats stay hidden
    items = await app.dialogs.get()
    assert [c.chat_type for c in items] == ["saved", "channel"]
    assert items[0] == ChatInfo(ME, SAVED_MESSAGES_TITLE, "saved")
    for q in ("收藏", "我的收藏", "saved", "SAVED MESSAGES", str(ME)):
        assert [c.chat_id for c in DialogCache.search(items, q)] == [ME], q
    assert DialogCache.search(items, "friend") == []


async def test_saved_messages_are_downloaded(app, base_dir) -> None:
    await app.monitors.add(ChatInfo(ME, SAVED_MESSAGES_TITLE, "saved"))
    msg = app.client.add(ME, make_message(5, name="note.pdf", mime="application/pdf"))
    jid = await app.monitors.handle_message(ME, msg)       # event.chat_id == own user id
    assert jid
    app.pool.start()
    await wait_until(lambda: _done(app, jid))
    row = await app.repo.db.fetchone("SELECT saved_path FROM downloads")
    path = Path(row[0])
    assert path.exists() and path.relative_to(base_dir.resolve()).parts[0] == "Saved Messages"


async def _done(app, jid):
    return (await app.repo.get_job(jid))["status"] == "done"


async def test_web_add_and_show_saved_messages(web, app, with_password) -> None:  # noqa: F811
    app.client.add_user_dialog(ME, "Josch", is_self=True)
    app.client.add_dialog(-1001, "Alpha channel")
    token = await logged_in(web)
    await web.get("/lang/zh-TW")
    html = (await web.get("/monitors/dialogs", params={"q": "收藏"})).text
    assert "我的收藏" in html and "Alpha" not in html
    r = await web.post("/monitors/add", data={"csrf_token": token, "chat_id": str(ME)})
    assert "ok=monitor_added" in r.headers["location"]
    page = (await web.get("/monitors")).text
    assert "<strong>我的收藏</strong>" in page and "Saved Messages" not in page.split("<main", 1)[1]
    await web.get("/lang/en")
    assert "<strong>Saved Messages</strong>" in (await web.get("/monitors")).text
