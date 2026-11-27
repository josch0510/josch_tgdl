"""Web authentication: server-side sessions, CSRF tokens, login lockout."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from ..passwords import needs_rehash, verify_password, hash_password
from ..storage.db import parse_iso
from ..storage.repo import Repository

SESSION_COOKIE = "tgdl_session"
LANG_COOKIE = "tgdl_lang"
CSRF_FIELD = "csrf_token"
CSRF_HEADER = "x-csrf-token"

SESSION_IDLE = timedelta(hours=2)
SESSION_ABSOLUTE = timedelta(hours=12)
TOUCH_EVERY = timedelta(seconds=60)

IP_MAX_FAILURES = 5
GLOBAL_MAX_FAILURES = 20
FAILURE_WINDOW = timedelta(minutes=15)
LOCKOUT = timedelta(minutes=15)
FAILED_LOGIN_DELAY = 0.5  # seconds


def _now() -> datetime:
    return datetime.now(timezone.utc)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class Session:
    token_hash: str
    csrf_token: str
    expires_at: datetime


class SessionManager:
    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    async def create(self, client_ip: str | None) -> tuple[str, Session]:
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        expires = _now() + SESSION_ABSOLUTE
        await self.repo.purge_sessions(_now() - SESSION_IDLE)
        await self.repo.create_session(hash_token(token), csrf, expires, client_ip)
        return token, Session(hash_token(token), csrf, expires)

    async def validate(self, token: str | None) -> Session | None:
        if not token or len(token) > 200:
            return None
        th = hash_token(token)
        row = await self.repo.get_session(th)
        if row is None:
            return None
        now = _now()
        expires = parse_iso(row["expires_at"])
        last_seen = parse_iso(row["last_seen"])
        if now >= expires or now - last_seen >= SESSION_IDLE:
            await self.repo.delete_session(th)
            return None
        if now - last_seen >= TOUCH_EVERY:
            await self.repo.touch_session(th)
        return Session(th, row["csrf_token"], expires)

    async def destroy(self, token: str | None) -> None:
        if token:
            await self.repo.delete_session(hash_token(token))


def csrf_ok(session: Session, supplied: str | None) -> bool:
    return bool(supplied) and hmac.compare_digest(session.csrf_token, supplied or "")


def origin_ok(origin: str | None, referer: str | None, host: str | None) -> bool:
    """Same-origin check on state-changing requests. Browsers send Origin on
    POST; if present it must match the Host header. Otherwise fall back to
    Referer when present."""
    source = origin if origin and origin != "null" else referer
    if origin == "null":
        return False
    if not source:
        return True  # no header at all (non-browser client): the CSRF token still applies
    if not host:
        return False
    return urlsplit(source).netloc.lower() == host.lower()


class LoginLimiter:
    """Per-IP and global failed-login counters, persisted in SQLite."""

    def __init__(self, repo: Repository) -> None:
        self.repo = repo
        self._lock = asyncio.Lock()

    async def locked_until(self, ip: str) -> datetime | None:
        now = _now()
        latest: datetime | None = None
        for scope in (f"ip:{ip}", "global"):
            row = await self.repo.get_login_failure(scope)
            if row and row["locked_until"]:
                until = parse_iso(row["locked_until"])
                if until > now and (latest is None or until > latest):
                    latest = until
        return latest

    async def record_failure(self, ip: str) -> bool:
        """Count a failure; returns True if this failure triggered a lockout."""
        async with self._lock:
            now = _now()
            triggered = False
            for scope, limit in ((f"ip:{ip}", IP_MAX_FAILURES), ("global", GLOBAL_MAX_FAILURES)):
                row = await self.repo.get_login_failure(scope)
                if row is None or now - parse_iso(row["window_start"]) > FAILURE_WINDOW:
                    count, start = 1, now
                else:
                    count, start = row["fail_count"] + 1, parse_iso(row["window_start"])
                locked = None
                if count >= limit:
                    locked = now + LOCKOUT
                    triggered = True
                    count, start = 0, now  # new window after the lockout
                elif row and row["locked_until"] and parse_iso(row["locked_until"]) > now:
                    locked = parse_iso(row["locked_until"])
                await self.repo.save_login_failure(scope, count, start, locked)
            return triggered

    async def record_success(self, ip: str) -> None:
        await self.repo.clear_login_failure(f"ip:{ip}")


async def check_password(repo: Repository, password: str) -> bool:
    """Verify the admin password off the event loop (argon2 is CPU-bound)."""
    stored = await repo.get_password_hash()
    ok = await asyncio.to_thread(verify_password, stored, password)
    if ok and stored and needs_rehash(stored):
        await repo.set_password_hash(await asyncio.to_thread(hash_password, password),
                                     revoke_sessions=False)
    return ok
