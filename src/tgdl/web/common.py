"""Shared web helpers: rendering, auth/CSRF dependencies, formatting."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from ..i18n import from_accept_language, get_translator, normalize_lang
from ..logging_setup import audit
from ..storage.db import parse_iso
from .security import (
    CSRF_FIELD, CSRF_HEADER, LANG_COOKIE, SESSION_COOKIE, Session, SessionManager, csrf_ok,
    origin_ok,
)

if TYPE_CHECKING:
    from ..app import App

log = logging.getLogger("tgdl.web")

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ------------------------------------------------------------- formatting
def human_size(n: Any) -> str:
    if n is None:
        return "-"
    v = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(v) < 1024 or unit == "TB":
            return f"{int(v)} B" if unit == "B" else f"{v:.1f} {unit}"
        v /= 1024
    return f"{v:.1f} TB"


def human_duration(seconds: Any) -> str:
    if seconds is None:
        return "-"
    s = int(max(0, float(seconds)))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def make_local_dt(app: "App") -> Any:
    def local_dt(value: Any, fmt: str = "%Y-%m-%d %H:%M") -> str:
        if not value:
            return "-"
        dt = value if isinstance(value, datetime) else parse_iso(str(value))
        return dt.astimezone(app.settings.tz).strftime(fmt)
    return local_dt


templates.env.filters["size"] = human_size
templates.env.filters["duration"] = human_duration


# ------------------------------------------------------------------ core
def core(request: Request) -> "App":
    return request.app.state.core


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "-"


def get_lang(request: Request) -> str:
    return (normalize_lang(request.cookies.get(LANG_COOKIE))
            or from_accept_language(request.headers.get("accept-language"))
            or core(request).settings.get("language"))


def translator_for(request: Request) -> Any:
    lang = get_lang(request)
    tr = get_translator()

    def t(key: str, **kw: Any) -> str:
        return tr.t(key, lang, **kw)
    return t


def safe_next(value: str | None) -> str:
    """Only allow local absolute paths as redirect targets."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    return value


def flash_message(request: Request) -> tuple[str, str] | None:
    tr = get_translator()
    for kind in ("ok", "err"):
        key = request.query_params.get(kind)
        if key and tr.has(f"flash.{key}"):
            return kind, f"flash.{key}"
    return None


def render(request: Request, name: str, status_code: int = 200, **ctx: Any) -> Response:
    app = core(request)
    session: Session | None = getattr(request.state, "session", None)
    ctx.update(
        t=translator_for(request),
        lang=get_lang(request),
        csrf_token=session.csrf_token if session else "",
        logged_in=session is not None,
        me_name=app.me_name,
        flash=flash_message(request),
        local_dt=make_local_dt(app),
        path=request.url.path,
        flood=[f for f in app.flood_status() if f["active"]],
    )
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def redirect(url: str, **flash: str) -> RedirectResponse:
    if flash:
        sep = "&" if "?" in url else "?"
        url += sep + "&".join(f"{k}={quote(v)}" for k, v in flash.items())
    return RedirectResponse(url, status_code=303)


def audit_web(request: Request, action: str, message: str = "", level: int = logging.INFO) -> None:
    audit(action, message, ip=client_ip(request), level=level)


# ------------------------------------------------------------------ auth
class LoginRequired(Exception):
    pass


async def require_session(request: Request) -> Session:
    sessions: SessionManager = request.app.state.sessions
    session = await sessions.validate(request.cookies.get(SESSION_COOKIE))
    if session is None:
        raise LoginRequired()
    request.state.session = session
    return session


async def require_csrf(request: Request, session: Session = Depends(require_session)) -> Session:
    """For every state-changing request: same-origin + synchronizer token."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return session
    if not origin_ok(request.headers.get("origin"), request.headers.get("referer"),
                     request.headers.get("host")):
        audit_web(request, "csrf_rejected", f"bad origin path={request.url.path}", logging.WARNING)
        raise HTTPException(status_code=403, detail="origin")
    supplied = request.headers.get(CSRF_HEADER)
    if not supplied:
        form = await request.form()
        value = form.get(CSRF_FIELD)
        supplied = value if isinstance(value, str) else None
    if not csrf_ok(session, supplied):
        audit_web(request, "csrf_rejected", f"bad token path={request.url.path}", logging.WARNING)
        raise HTTPException(status_code=403, detail="csrf")
    return session
