"""Command line entry point: ``tgdl login | set-password | serve | fetch``."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import sys
from pathlib import Path
from collections.abc import Callable
from typing import Any

from .config import AppConfig, ConfigError, load_config
from .i18n import cli_lang, get_translator
from .logging_setup import audit, log_event, setup_logging
from .passwords import MIN_PASSWORD_LENGTH, hash_password
from .settings import SettingsService
from .storage.db import Database
from .storage.repo import Repository

log = logging.getLogger("tgdl.cli")

Translate = Callable[..., str]


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024
    return f"{n:.1f}TB"


async def open_storage(cfg: AppConfig) -> tuple[Database, Repository, SettingsService]:
    cfg.prepare_dirs()
    db = Database(cfg.db_path)
    await db.connect()
    repo = Repository(db)
    settings = SettingsService(cfg, repo)
    await settings.load()
    return db, repo, settings


# ---------------------------------------------------------------- commands
async def cmd_login(cfg: AppConfig, t: Translate, _args: argparse.Namespace) -> int:
    from telethon import utils

    from .telegram.client import build_client, interactive_login

    client = build_client(cfg)
    try:
        me = await interactive_login(client, t)
        print(t("cli.login.success", name=utils.get_display_name(me)))
    finally:
        await client.disconnect()
    return 0


async def cmd_set_password(cfg: AppConfig, t: Translate, _args: argparse.Namespace) -> int:
    pw1 = getpass.getpass(t("cli.password.prompt", min=MIN_PASSWORD_LENGTH))
    if len(pw1) < MIN_PASSWORD_LENGTH:
        print(t("cli.password.too_short", min=MIN_PASSWORD_LENGTH))
        return 1
    pw2 = getpass.getpass(t("cli.password.confirm"))
    if pw1 != pw2:
        print(t("cli.password.mismatch"))
        return 1
    db, repo, _ = await open_storage(cfg)
    try:
        await repo.set_password_hash(hash_password(pw1))
    finally:
        await db.close()
    audit("set_password", "Admin password set from CLI; all web sessions revoked", ip="cli")
    print(t("cli.password.done"))
    return 0


async def cmd_fetch(cfg: AppConfig, t: Translate, args: argparse.Namespace) -> int:
    """One-shot: download media from the latest N messages of one chat."""
    from .downloader.pipeline import Pipeline
    from .telegram.client import build_client, resolve_chat

    client = build_client(cfg)  # may raise ConfigError: create before opening the DB
    db, repo, settings = await open_storage(cfg)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            print(t("cli.not_logged_in"))
            return 1
        chat = await resolve_chat(client, args.chat)
        print(t("cli.fetch.start", chat=chat.title, limit=args.limit))
        pipeline = Pipeline(cfg, repo, settings)
        monitor = await repo.get_monitor(chat.chat_id)
        ok = skipped = failed = 0
        async for message in client.iter_messages(chat.chat_id, limit=args.limit):
            job_id, reason = await pipeline.consider(chat_id=chat.chat_id, chat_title=chat.title,
                                                     message=message, source="manual",
                                                     monitor=monitor)
            if job_id is None:
                if reason != "no_media":
                    skipped += 1
                continue
            await repo.set_job_status(job_id, "running", count_attempt=True)
            job = await repo.get_job(job_id)
            assert job is not None

            def progress(done: int, total: int, *, mid: int = message.id) -> None:
                pct = f"{done * 100 // total}%" if total else "?"
                print(f"\r  #{mid} {pct} {_human(done)}/{_human(total or 0)}   ", end="", flush=True)

            try:
                path = await pipeline.execute(client, job, message, progress=progress)
                print()
                if path is None:
                    skipped += 1
                else:
                    ok += 1
                    print(t("cli.fetch.saved", path=str(path)))
            except Exception as exc:  # noqa: BLE001 - report and continue
                print()
                failed += 1
                await repo.set_job_status(job_id, "failed", error=type(exc).__name__)
                log_event(log, logging.ERROR, "download_failed", repr(exc),
                          chat=chat.chat_id, msg_id=message.id, reason=type(exc).__name__)
                print(t("cli.fetch.failed", msg_id=message.id, error=type(exc).__name__))
        print(t("cli.fetch.summary", ok=ok, skipped=skipped, failed=failed))
        return 0 if failed == 0 else 2
    finally:
        await client.disconnect()
        await db.close()


async def cmd_monitor(cfg: AppConfig, t: Translate, args: argparse.Namespace) -> int:
    """Manage monitors from the terminal (the web UI is the main way).
    A running `tgdl serve` picks up changes within 30 seconds."""
    db, repo, _ = await open_storage(cfg)
    try:
        if args.action == "list":
            rows = await repo.list_monitors()
            if not rows:
                print(t("cli.monitor.empty"))
            for m in rows:
                state = t("cli.monitor.on") if m["enabled"] else t("cli.monitor.off")
                print(f"{m['chat_id']:>16}  [{state}]  {m['chat_title']}")
            return 0
        if not args.chat:
            print(t("cli.monitor.need_chat"))
            return 1
        if args.action == "add":
            from .telegram.client import build_client, resolve_chat

            client = build_client(cfg)
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    print(t("cli.not_logged_in"))
                    return 1
                chat = await resolve_chat(client, args.chat)
            finally:
                await client.disconnect()
            await repo.add_monitor(chat.chat_id, chat.title, chat.chat_type)
            audit("monitor_add", f"chat={chat.chat_id}", ip="cli")
            print(t("cli.monitor.added", chat=chat.title, chat_id=chat.chat_id))
            return 0
        try:
            chat_id = int(args.chat)
        except ValueError:
            print(t("cli.monitor.need_id"))
            return 1
        if await repo.get_monitor(chat_id) is None:
            print(t("cli.monitor.not_found", chat_id=chat_id))
            return 1
        if args.action == "remove":
            await repo.delete_monitor(chat_id)
        else:
            await repo.update_monitor(chat_id, enabled=args.action == "enable")
        audit(f"monitor_{args.action}", f"chat={chat_id}", ip="cli")
        print(t("cli.monitor.done"))
        return 0
    finally:
        await db.close()


async def cmd_serve(cfg: AppConfig, t: Translate, args: argparse.Namespace) -> int:
    from .app import run_server

    return await run_server(cfg, t, args)


COMMANDS: dict[str, Callable[[AppConfig, Translate, argparse.Namespace], Any]] = {
    "login": cmd_login,
    "set-password": cmd_set_password,
    "serve": cmd_serve,
    "fetch": cmd_fetch,
    "monitor": cmd_monitor,
}


def build_parser(t: Translate) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tgdl", description=t("cli.description"))
    p.add_argument("--lang", choices=["en", "zh-TW"], help=t("cli.opt.lang"))
    p.add_argument("--home", help=t("cli.opt.home"))
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("login", help=t("cli.cmd.login"))
    sub.add_parser("set-password", help=t("cli.cmd.set_password"))
    sub.add_parser("serve", help=t("cli.cmd.serve"))
    f = sub.add_parser("fetch", help=t("cli.cmd.fetch"))
    f.add_argument("chat", help=t("cli.opt.chat"))
    f.add_argument("--limit", type=int, default=20, help=t("cli.opt.limit"))
    mon = sub.add_parser("monitor", help=t("cli.cmd.monitor"))
    mon.add_argument("action", choices=["list", "add", "remove", "enable", "disable"])
    mon.add_argument("chat", nargs="?", help=t("cli.opt.chat"))
    return p


def _pre_lang(argv: list[str]) -> str | None:
    for i, a in enumerate(argv):
        if a == "--lang" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--lang="):
            return a.split("=", 1)[1]
    return None


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    tr = get_translator()
    lang = cli_lang(_pre_lang(argv))

    def t(key: str, **kw: Any) -> str:
        return tr.t(key, lang, **kw)

    args = build_parser(t).parse_args(argv)

    try:
        cfg = load_config(Path(args.home) if args.home else None)
    except ConfigError as exc:
        print(t(exc.code, **exc.params), file=sys.stderr)
        return 1
    cfg.prepare_dirs()
    setup_logging(cfg.logs_dir, console=args.command == "serve")
    try:
        return asyncio.run(COMMANDS[args.command](cfg, t, args)) or 0
    except ConfigError as exc:
        print(t(exc.code, **exc.params), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
