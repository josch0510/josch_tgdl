import asyncio
import re

import pytest

from tgdl.passwords import hash_password
from tgdl.web import security

PASSWORD = "correct horse battery"
USERNAME = "josch"


def hidden(html: str, name: str) -> str:
    m = re.search(rf'name="{name}" value="([^"]*)"', html)
    assert m, f"no hidden field {name}"
    return m.group(1)


def csrf_of(html: str) -> str:
    return re.search(r'<meta name="csrf-token" content="([^"]*)"', html).group(1)


@pytest.fixture(autouse=True)
def no_login_delay(monkeypatch):
    monkeypatch.setattr(security, "FAILED_LOGIN_DELAY", 0)


@pytest.fixture
async def with_password(repo):
    await repo.set_admin(USERNAME, hash_password(PASSWORD))


async def login(web, password=PASSWORD, username=USERNAME, **extra):
    r = await web.get("/login")
    data = {"username": username, "password": password, "login_csrf": hidden(r.text, "login_csrf"),
            "next": "/"}
    data.update(extra)
    return await web.post("/login", data=data)


async def logged_in(web) -> str:
    r = await login(web)
    assert r.status_code == 303, r.text
    page = await web.get("/")
    assert page.status_code == 200
    return csrf_of(page.text)


# ------------------------------------------------------------ auth
async def test_requires_login(web) -> None:
    for path in ("/", "/monitors", "/settings", "/events", "/partials/dashboard"):
        r = await web.get(path)
        assert r.status_code == 303 and r.headers["location"].startswith("/login"), path
    r = await web.get("/partials/dashboard", headers={"HX-Request": "true"})
    assert r.status_code == 401 and r.headers["HX-Redirect"] == "/login"


async def test_login_page_without_account(web) -> None:
    r = await web.get("/login")
    assert "set-password" in r.text


async def test_account_without_username_cannot_log_in(web, repo) -> None:
    """A v1 database upgraded to v2 has a password but no username yet."""
    await repo.set_admin(USERNAME, hash_password(PASSWORD))
    await repo.db.execute("UPDATE admin SET username = NULL")
    assert "set-password" in (await web.get("/login")).text
    for name in ("", "admin", "None"):
        assert (await login(web, username=name)).status_code == 401


async def test_username_required_and_checked(web, with_password) -> None:
    assert (await login(web, username="someone_else")).status_code == 401
    assert (await login(web, username="")).status_code == 401
    r = await login(web, username="  JOSCH ")      # case-insensitive, trimmed
    assert r.status_code == 303


async def test_failed_login_keeps_username_and_audits_it(web, app, with_password) -> None:
    from tgdl.logging_setup import setup_logging
    setup_logging(app.cfg.logs_dir, console=False)
    r = await login(web, username="josch", password="wrong")
    assert 'name="username" value="josch"' in r.text
    await login(web, username="<b>x</b>\x07", password="wrong")
    for h in __import__("logging").getLogger("tgdl_audit").handlers:
        h.flush()
    audit_text = (app.cfg.logs_dir / "audit.log").read_text(encoding="utf-8")
    assert "user='josch' | ip=127.0.0.1 | action=login_failed" in audit_text
    assert "\x07" not in audit_text and PASSWORD not in audit_text and "wrong" not in audit_text
    # actions after login are attributed to the real account
    await login(web)
    token = csrf_of((await web.get("/")).text)
    await web.post("/logout", data={"csrf_token": token})
    for h in __import__("logging").getLogger("tgdl_audit").handlers:
        h.flush()
    audit_text = (app.cfg.logs_dir / "audit.log").read_text(encoding="utf-8")
    assert "user=josch | ip=127.0.0.1 | action=login_success" in audit_text
    assert "user=josch | ip=127.0.0.1 | action=logout" in audit_text


async def test_login_success_sets_secure_cookie(web, with_password) -> None:
    r = await login(web)
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = [h for h in r.headers.get_list("set-cookie") if h.startswith("tgdl_session=")][0]
    low = cookie.lower()
    assert "httponly" in low and "samesite=strict" in low and "max-age=" in low


async def test_wrong_password(web, with_password) -> None:
    r = await login(web, password="nope")
    assert r.status_code == 401
    assert "tgdl_session" not in r.headers.get("set-cookie", "")


async def test_login_csrf_required(web, with_password) -> None:
    await web.get("/login")
    r = await web.post("/login", data={"username": USERNAME, "password": PASSWORD, "login_csrf": "forged"})
    assert r.status_code == 400


async def test_lockout_after_failures(web, with_password) -> None:
    for _ in range(security.IP_MAX_FAILURES):
        assert (await login(web, password="bad")).status_code == 401
    r = await login(web)  # even the right password is refused while locked
    assert r.status_code == 429


async def test_open_redirect_blocked(web, with_password) -> None:
    r = await login(web, next="//evil.example/x")
    assert r.headers["location"] == "/"
    r = await web.get("/lang/en", params={"next": "https://evil.example"})
    assert r.headers["location"] == "/"


async def test_logout_invalidates_session(web, with_password) -> None:
    token = await logged_in(web)
    old_cookie = web.cookies.get("tgdl_session")
    r = await web.post("/logout", data={"csrf_token": token})
    assert r.status_code == 303
    web.cookies.set("tgdl_session", old_cookie)
    assert (await web.get("/")).status_code == 303


async def test_password_change_revokes_sessions(web, repo, with_password) -> None:
    await logged_in(web)
    await repo.set_admin(USERNAME, hash_password("another long password"))
    assert (await web.get("/")).status_code == 303


# ---------------------------------------------------- CSRF / headers
async def test_csrf_required_on_post(web, with_password) -> None:
    token = await logged_in(web)
    r = await web.post("/settings", data={"concurrency": "5"})
    assert r.status_code == 403
    r = await web.post("/settings", data={"csrf_token": "wrong"})
    assert r.status_code == 403
    r = await web.post("/logout", data={"csrf_token": token}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    r = await web.post("/logout", headers={"X-CSRF-Token": token, "Origin": "http://localhost:8080"})
    assert r.status_code == 303


async def test_security_headers(web) -> None:
    r = await web.get("/login")
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"
    assert "<script>" not in r.text and " onclick=" not in r.text and " style=" not in r.text


@pytest.mark.parametrize("host, ok", [
    ("localhost:8080", True), ("LOCALHOST:8080", True), ("127.0.0.1:8080", True),
    ("[::1]:8080", True), ("localhost:18080", True),
    ("192.168.1.20:8080", True), ("10.0.0.5", True), ("[fe80::1]:8080", True),  # IPs: no rebinding
    ("evil.example", False), ("evil.example:8080", False), ("debian.lan:8080", False),
    ("localhost.evil.example", False), ("", False), ("a b", False), ("x:y:z", False),
])
async def test_host_header_check(web, host, ok) -> None:
    r = await web.get("/login", headers={"Host": host})
    assert (r.status_code != 400) is ok, host
    if not ok:
        assert "ALLOWED_HOSTS" in r.text


async def test_rejected_host_is_logged(web, app) -> None:
    from tgdl.logging_setup import ring_buffer, setup_logging
    setup_logging(app.cfg.logs_dir, console=False)
    await web.get("/login", headers={"Host": "debian.lan:8080"})
    assert any("host_rejected" in e["line"] and "debian.lan" in e["line"] for e in ring_buffer.records)


def test_own_hostname_allowed(monkeypatch, tmp_path) -> None:
    import socket

    from tgdl import config
    from tgdl.web.server import host_allowed
    monkeypatch.setattr(socket, "gethostname", lambda: "Debian")
    monkeypatch.setattr(socket, "getfqdn", lambda name="": "debian.home.lan")
    config.local_host_names.cache_clear()
    try:
        cfg = config.AppConfig(home=tmp_path, api_id=None, api_hash=None,
                               allowed_base_dirs=(tmp_path,), web_host="0.0.0.0", web_port=5000)
        allowed = {h.strip("[]") for h in cfg.allowed_hosts}
        assert host_allowed("debian:5000", allowed)
        assert host_allowed("debian.home.lan:5000", allowed)
        assert not host_allowed("evil.example:5000", allowed)
    finally:
        config.local_host_names.cache_clear()


async def test_allowed_hosts_from_env(tmp_path) -> None:
    import httpx

    from tgdl.config import load_config
    (tmp_path / ".env").write_text(f"ALLOWED_HOSTS=Debian.LAN, nas.local\nALLOWED_BASE_DIRS={tmp_path}\n")
    cfg = load_config(tmp_path)
    from tgdl.web.server import host_allowed
    allowed = {h.strip("[]") for h in cfg.allowed_hosts}
    assert host_allowed("debian.lan:8080", allowed) and host_allowed("nas.local", allowed)
    assert not host_allowed("other.lan", allowed)


async def test_no_secrets_in_pages(web, app, with_password) -> None:
    await logged_in(web)
    for path in ("/", "/monitors", "/settings"):
        assert app.cfg.api_hash not in (await web.get(path)).text


# ------------------------------------------------------------ i18n
async def test_language_switch(web, with_password) -> None:
    await web.get("/lang/zh-TW", params={"next": "/login"})
    assert "管理員密碼" in (await web.get("/login")).text
    await web.get("/lang/en")
    assert "Admin password" in (await web.get("/login")).text


async def test_accept_language_default(web) -> None:
    r = await web.get("/login", headers={"Accept-Language": "en-US,en;q=0.9"})
    assert "Admin password" in r.text


# -------------------------------------------------------- settings
async def test_settings_update_applies_live(web, app, base_dir, with_password) -> None:
    token = await logged_in(web)
    form = (await web.get("/settings")).text
    data = {
        "csrf_token": token, "default_dir": str(base_dir / "media"), "concurrency": "7",
        "path_template": "{base_dir}/{chat_name}/{filename}", "timezone": "UTC",
        "default_media_types": ["photo", "video"], "default_prefix": "{date}_",
        "language": "en", "mime": "image/ = Photos\nvideo/ = Videos",
    }
    for k in ("photo", "video", "video_note", "animation", "audio", "voice", "sticker"):
        data[f"kind_{k}"] = re.search(rf'name="kind_{k}">.*?<option value="(\w+)" selected', form, re.S).group(1)
    for c in ("Photos", "Videos", "Audio", "Voice", "Documents", "Archives", "Others"):
        data[f"ext_{c}"] = ""
    data["ext_Archives"] = "zip, rar"
    r = await web.post("/settings", data=data)
    assert r.status_code == 303 and "ok=settings_saved" in r.headers["location"]
    assert app.settings.get("concurrency") == 7 and app.pool.target == 7
    assert app.settings.get("timezone") == "UTC"
    assert app.settings.category_map["extensions"]["Archives"] == ["zip", "rar"]


@pytest.mark.parametrize("field, value, code", [
    ("default_dir", "/etc", "dir.outside_allowed"),
    ("default_dir", "../../etc", "dir.not_absolute"),
    ("concurrency", "11", "settings.concurrency_range"),
    ("path_template", "/tmp/{filename}", "template.must_start_with_base_dir"),
    ("timezone", "Mars/Olympus", "settings.bad_timezone"),
])
async def test_settings_validation(web, app, base_dir, with_password, field, value, code) -> None:
    from tgdl.i18n import get_translator
    token = await logged_in(web)
    s = app.settings.all()
    data = {"csrf_token": token, "default_dir": s["default_dir"], "concurrency": "3",
            "path_template": s["path_template"], "timezone": s["timezone"],
            "default_media_types": s["default_media_types"], "language": "en", "mime": "image/ = Photos"}
    data[field] = value
    before = app.settings.all()
    await web.get("/lang/en")
    r = await web.post("/settings", data=data)
    assert r.status_code == 400
    assert get_translator().t(code, "en").replace("'", "&#39;") in r.text
    assert app.settings.all() == before


# -------------------------------------------------------- monitors
async def test_monitor_lifecycle(web, app, base_dir, with_password) -> None:
    app.client.add_dialog(-1001, "貓咪頻道")
    app.client.add_dialog(-1002, "Dev Group", group=True)
    token = await logged_in(web)

    r = await web.get("/monitors/dialogs", params={"q": "貓"})
    assert "貓咪頻道" in r.text and "Dev Group" not in r.text

    r = await web.post("/monitors/add", data={"csrf_token": token, "chat_id": "-1001"})
    assert r.status_code == 303 and "ok=monitor_added" in r.headers["location"]
    assert app.monitors.monitors[-1001]["enabled"]

    # a chat id that is not in the user's dialog list is refused
    r = await web.post("/monitors/add", data={"csrf_token": token, "chat_id": "-999"})
    assert "err=chat_not_found" in r.headers["location"]

    r = await web.post("/monitors/-1001/toggle", data={"csrf_token": token})
    assert not app.monitors.monitors[-1001]["enabled"]

    # per-chat settings: directory outside the allowed roots is rejected
    r = await web.post("/monitors/-1001/settings", data={
        "csrf_token": token, "download_dir": str(base_dir / ".." / ".." / "etc"),
        "use_default_types": "1", "use_default_prefix": "1"})
    assert r.status_code == 400
    r = await web.post("/monitors/-1001/settings", data={
        "csrf_token": token, "download_dir": str(base_dir / "cats"), "media_types": ["photo"],
        "filename_prefix": "{chat_name}_", "min_mb": "0.5", "max_mb": "100", "keyword": "cute"})
    assert r.status_code == 303
    m = app.monitors.monitors[-1001]
    assert m["media_types"] == ["photo"] and m["download_dir"] == str(base_dir.resolve() / "cats")
    assert m["min_size"] == 512 * 1024 and m["keyword"] == "cute"

    r = await web.post("/monitors/-1001/settings", data={
        "csrf_token": token, "min_mb": "10", "max_mb": "1", "use_default_types": "1",
        "use_default_prefix": "1"})
    assert r.status_code == 400

    page = await web.get("/monitors")
    assert "貓咪頻道" in page.text

    r = await web.post("/monitors/-1001/delete", data={"csrf_token": token})
    assert "ok=monitor_removed" in r.headers["location"] and -1001 not in app.monitors.monitors


async def test_dialog_list_is_cached(web, app, with_password) -> None:
    app.client.add_dialog(-1, "A")
    await logged_in(web)
    await web.get("/monitors/dialogs")
    await web.get("/monitors/dialogs", params={"q": "a"})
    assert app.client.dialog_calls == 1
    await web.get("/monitors/dialogs", params={"refresh": "1"})
    assert app.client.dialog_calls == 2


async def test_backfill_route(web, app, with_password) -> None:
    app.client.add_dialog(-1001, "Chan")
    token = await logged_in(web)
    await web.get("/monitors/dialogs")
    await web.post("/monitors/add", data={"csrf_token": token, "chat_id": "-1001"})
    r = await web.post("/monitors/-1001/backfill", data={"csrf_token": token, "date_from": "2026-02-01",
                                                         "date_to": "2026-01-01"})
    assert "err=bad_range" in r.headers["location"]
    r = await web.post("/monitors/-1001/backfill", data={"csrf_token": token, "date_from": "2026-01-01",
                                                         "date_to": "2026-01-31"})
    assert "ok=backfill_started" in r.headers["location"]


# ------------------------------------------------------ dashboard
async def test_dashboard_shows_flood_wait(web, app, with_password) -> None:
    await logged_in(web)
    app.download_gate.trigger(120)
    r = await web.get("/partials/dashboard")
    assert 'class="countdown"' in r.text and "data-until=" in r.text
    r = await web.get("/monitors")  # global banner on other pages
    assert 'class="countdown"' in r.text


async def test_sse_stream(web, app, with_password) -> None:
    await logged_in(web)

    async def produce():
        await asyncio.sleep(0.2)
        app.bus.publish("queue", pending=3)
        app.bus.publish("internal_only", x=1)
        await asyncio.sleep(0.2)
        app.shutting_down.set()

    task = asyncio.create_task(produce())
    r = await web.get("/events")
    await task
    assert r.headers["content-type"].startswith("text/event-stream")
    assert 'event: queue\ndata: {"pending": 3}' in r.text
    assert "internal_only" not in r.text


async def test_every_form_carries_the_csrf_token(web, app, with_password) -> None:
    """Regression: macros imported without context rendered an empty token."""
    app.client.add_dialog(-1001, "Chan")
    token = await logged_in(web)
    await web.get("/monitors/dialogs")
    await web.post("/monitors/add", data={"csrf_token": token, "chat_id": "-1001"})
    pages = ["/", "/monitors", "/settings", "/monitors/-1001/settings", "/monitors/dialogs"]
    checked = 0
    for path in pages:
        html = (await web.get(path)).text
        for form in re.findall(r'<form method="post".*?</form>', html, re.S):
            values = re.findall(r'name="csrf_token" value="([^"]*)"', form)
            assert values == [token], f"{path}: {form[:120]}"
            checked += 1
    assert checked >= 6


async def test_backfill_range_displayed_inclusive(web, app, with_password) -> None:
    app.client.add_dialog(-1001, "Chan")
    token = await logged_in(web)
    await web.get("/monitors/dialogs")
    await web.post("/monitors/add", data={"csrf_token": token, "chat_id": "-1001"})
    await web.post("/monitors/-1001/backfill", data={"csrf_token": token, "date_from": "2026-01-01",
                                                     "date_to": "2026-01-31"})
    await asyncio.sleep(0.1)
    html = (await web.get("/monitors")).text
    assert "2026-01-01 – 2026-01-31" in html
