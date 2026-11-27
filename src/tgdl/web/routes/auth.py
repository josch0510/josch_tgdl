"""Login / logout / language switch."""

from __future__ import annotations

import asyncio
import hmac
import logging
import math
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response

from ...i18n import SUPPORTED
from ..common import (
    audit_web, client_ip, core, redirect, render, require_csrf, safe_next,
)
from ..security import (
    FAILED_LOGIN_DELAY, LANG_COOKIE, SESSION_ABSOLUTE, SESSION_COOKIE, LoginLimiter, Session,
    SessionManager, check_password,
)

router = APIRouter()

LOGIN_CSRF_COOKIE = "tgdl_login_csrf"


def _login_page(request: Request, *, error: str | None = None, status_code: int = 200,
                **params: object) -> Response:
    token = secrets.token_urlsafe(32)
    resp = render(request, "login.html", status_code=status_code, login_csrf=token,
                  error=error, error_params=params,
                  next=safe_next(request.query_params.get("next")))
    resp.set_cookie(LOGIN_CSRF_COOKIE, token, max_age=3600, httponly=True, samesite="strict",
                    secure=core(request).cfg.cookie_secure, path="/login")
    return resp


@router.get("/login")
async def login_form(request: Request) -> Response:
    sessions: SessionManager = request.app.state.sessions
    if await sessions.validate(request.cookies.get(SESSION_COOKIE)):
        return RedirectResponse("/", status_code=303)
    if await core(request).repo.get_password_hash() is None:
        return _login_page(request, error="login.no_password")
    return _login_page(request)


@router.post("/login")
async def login_submit(request: Request, password: str = Form(""), login_csrf: str = Form(""),
                       next: str = Form("/")) -> Response:
    app = core(request)
    limiter: LoginLimiter = request.app.state.limiter
    sessions: SessionManager = request.app.state.sessions
    ip = client_ip(request)

    cookie_token = request.cookies.get(LOGIN_CSRF_COOKIE, "")
    if not cookie_token or not hmac.compare_digest(cookie_token, login_csrf):
        audit_web(request, "login_csrf_rejected", level=logging.WARNING)
        return _login_page(request, error="login.expired", status_code=400)

    locked = await limiter.locked_until(ip)
    if locked is not None:
        minutes = max(1, math.ceil((locked - datetime.now(timezone.utc)).total_seconds() / 60))
        audit_web(request, "login_blocked", f"locked for {minutes} more minute(s)", logging.WARNING)
        return _login_page(request, error="login.locked", status_code=429, minutes=minutes)

    if len(password) > 1024 or not await check_password(app.repo, password):
        lockout = await limiter.record_failure(ip)
        audit_web(request, "login_failed", "lockout triggered" if lockout else "", logging.WARNING)
        await asyncio.sleep(FAILED_LOGIN_DELAY)
        return _login_page(request, error="login.invalid", status_code=401)

    await limiter.record_success(ip)
    token, _ = await sessions.create(ip)
    audit_web(request, "login_success")
    resp = RedirectResponse(safe_next(next), status_code=303)
    resp.set_cookie(SESSION_COOKIE, token, max_age=int(SESSION_ABSOLUTE.total_seconds()),
                    httponly=True, samesite="strict", secure=app.cfg.cookie_secure, path="/")
    resp.delete_cookie(LOGIN_CSRF_COOKIE, path="/login")
    return resp


@router.post("/logout")
async def logout(request: Request, _s: Session = Depends(require_csrf)) -> Response:
    sessions: SessionManager = request.app.state.sessions
    await sessions.destroy(request.cookies.get(SESSION_COOKIE))
    audit_web(request, "logout")
    resp = redirect("/login")
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@router.get("/lang/{code}")
async def set_lang(request: Request, code: str, next: str = "/") -> Response:
    """Per-browser UI language (a preference, not a security-relevant state)."""
    resp = RedirectResponse(safe_next(next), status_code=303)
    if code in SUPPORTED:
        resp.set_cookie(LANG_COOKIE, code, max_age=365 * 86400, httponly=True, samesite="lax",
                        secure=core(request).cfg.cookie_secure, path="/")
    return resp
