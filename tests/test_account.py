"""Admin account: username rules, schema migration, CLI set-password."""
import argparse
import sqlite3
from pathlib import Path

import pytest

from tgdl.passwords import hash_password, normalize_username, usernames_match
from tgdl.storage.db import SCHEMA_VERSION, Database
from tgdl.storage.repo import Repository


@pytest.mark.parametrize("raw, ok", [
    ("admin", "admin"), ("  josch ", "josch"), ("j.sch-01@corp", "j.sch-01@corp"), ("管理員", "管理員"),
    ("", None), ("a b", None), ("x" * 65, None), ("ad'min", None), ("<script>", None), ("a\nb", None),
])
def test_normalize_username(raw, ok) -> None:
    assert normalize_username(raw) == ok


def test_usernames_match() -> None:
    assert usernames_match("Josch", "josch") and usernames_match("josch", " JOSCH ")
    assert not usernames_match(None, "") and not usernames_match("", "") and not usernames_match("a", "b")


async def test_migrates_v1_database(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)  # schema v1: admin table without username
    con.executescript("""
        CREATE TABLE admin (id INTEGER PRIMARY KEY CHECK (id = 1), password_hash TEXT NOT NULL,
                            updated_at TEXT NOT NULL);
        INSERT INTO admin VALUES (1, 'oldhash', '2026-01-01T00:00:00+00:00');
        CREATE TABLE web_sessions (token_hash TEXT PRIMARY KEY, csrf_token TEXT NOT NULL,
            created_at TEXT NOT NULL, expires_at TEXT NOT NULL, last_seen TEXT NOT NULL, client_ip TEXT);
        CREATE TABLE monitors (chat_id INTEGER PRIMARY KEY, chat_title TEXT NOT NULL,
            chat_type TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, media_types TEXT,
            download_dir TEXT, filename_prefix TEXT, min_size INTEGER, max_size INTEGER,
            keyword TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        INSERT INTO monitors (chat_id, chat_title, chat_type, created_at, updated_at)
            VALUES (-100, 'Old chat', 'channel', 'x', 'x');
        PRAGMA user_version = 1;
    """)
    con.close()
    db = Database(path)
    await db.connect()
    try:
        assert (await db.fetchone("PRAGMA user_version"))[0] == SCHEMA_VERSION
        repo = Repository(db)
        admin = await repo.get_admin()
        assert admin == {"username": None, "password_hash": "oldhash"}   # data kept
        m = await repo.get_monitor(-100)
        assert m["chat_title"] == "Old chat" and m["direction"] is None
        assert await repo.list_backfills() == []           # tables missing in v1 are created
    finally:
        await db.close()
    db = Database(path)          # second start: no error, no double migration
    await db.connect()
    await db.close()


async def test_set_admin_revokes_sessions(repo) -> None:
    await repo.create_session("t", "c", __import__("datetime").datetime(2099, 1, 1,
                              tzinfo=__import__("datetime").timezone.utc), "1.2.3.4")
    await repo.set_admin("josch", hash_password("a long password"))
    assert await repo.get_session("t") is None
    assert (await repo.get_admin())["username"] == "josch"


async def test_rehash_keeps_sessions(repo) -> None:
    await repo.set_admin("josch", hash_password("a long password"))
    await repo.create_session("t", "c", __import__("datetime").datetime(2099, 1, 1,
                              tzinfo=__import__("datetime").timezone.utc), None)
    await repo.update_password_hash("newhash")
    assert await repo.get_session("t") is not None
    assert (await repo.get_admin()) == {"username": "josch", "password_hash": "newhash"}


async def test_cli_set_password(cfg, monkeypatch, capsys) -> None:
    from tgdl import cli
    answers = iter(["correct horse battery", "correct horse battery"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": next(answers))
    monkeypatch.setattr("builtins.input", lambda prompt="": "")   # accept default "admin"
    t = lambda k, **kw: k  # noqa: E731
    assert await cli.cmd_set_password(cfg, t, argparse.Namespace(username=None)) == 0
    db = Database(cfg.db_path)
    await db.connect()
    try:
        admin = await Repository(db).get_admin()
    finally:
        await db.close()
    assert admin["username"] == "admin" and admin["password_hash"].startswith("$argon2id$")

    answers = iter(["another long password", "another long password"])
    assert await cli.cmd_set_password(cfg, t, argparse.Namespace(username="josch")) == 0
    assert await cli.cmd_set_password(cfg, t, argparse.Namespace(username="bad name")) == 1
    assert "cli.password.bad_username" in capsys.readouterr().out


def test_failed_migration_does_not_hang_at_exit(tmp_path: Path) -> None:
    """Regression: an exception inside Database.connect() left the aiosqlite
    thread running, so the process never exited."""
    import subprocess
    import sys
    path = tmp_path / "broken.db"
    con = sqlite3.connect(path)
    # v1 database whose admin "table" is a view: ALTER TABLE fails
    con.executescript("CREATE VIEW admin AS SELECT 1 AS id; PRAGMA user_version = 1;")
    con.close()
    code = ("import asyncio, sys\nfrom pathlib import Path\nfrom tgdl.storage.db import Database\n"
            "async def main():\n    try:\n        await Database(Path(sys.argv[1])).connect()\n"
            "    except Exception as e:\n        print('error:', type(e).__name__)\n"
            "asyncio.run(main())\n")
    r = subprocess.run([sys.executable, "-c", code, str(path)], capture_output=True, text=True, timeout=20)
    assert r.returncode == 0 and "error:" in r.stdout
