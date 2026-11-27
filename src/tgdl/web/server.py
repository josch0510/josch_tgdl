"""FastAPI app factory and in-loop uvicorn startup."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..logging_setup import log_event
from .common import STATIC_DIR, LoginRequired, render
from .security import LoginLimiter, SessionManager

if TYPE_CHECKING:
    from ..app import App

log = logging.getLogger("tgdl.web")

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; font-src 'self'; object-src 'none'; frame-ancestors 'none'; "
       "base-uri 'none'; form-action 'self'")
SECURITY_HEADERS = [
    (b"content-security-policy", CSP.encode()),
    (b"x-frame-options", b"DENY"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"same-origin"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=()"),
]


class SecurityHeadersMiddleware:
    """Pure ASGI (does not buffer streaming/SSE responses)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        is_static = scope["path"].startswith("/static/")

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                existing = {k.lower() for k, _ in headers}
                headers += [(k, v) for k, v in SECURITY_HEADERS if k not in existing]
                if not is_static and b"cache-control" not in existing:
                    headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


def create_app(core: "App") -> FastAPI:
    from .routes import auth, dashboard, history, logs, monitors, settings

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.core = core
    app.state.sessions = SessionManager(core.repo)
    app.state.limiter = LoginLimiter(core.repo)

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=core.cfg.allowed_hosts)
    app.add_middleware(SecurityHeadersMiddleware)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    for module in (auth, dashboard, monitors, settings, history, logs):
        app.include_router(module.router)

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, _exc: LoginRequired) -> Response:
        if request.headers.get("hx-request"):
            return Response(status_code=401, headers={"HX-Redirect": "/login"})
        target = request.url.path
        if request.method == "GET" and target not in ("/", "/login"):
            return RedirectResponse(f"/login?next={target}", status_code=303)
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException) -> Response:
        if exc.status_code == 403:
            return PlainTextResponse("Forbidden", status_code=403)
        if exc.status_code == 404:
            return PlainTextResponse("Not found", status_code=404)
        return PlainTextResponse(str(exc.detail), status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def _server_error(request: Request, exc: Exception) -> Response:
        log_event(log, logging.ERROR, "web_error", f"path={request.url.path} error={exc!r}",
                  reason=type(exc).__name__)
        return PlainTextResponse("Internal error", status_code=500)

    _ = render  # re-exported for routes
    return app


class _InLoopServer(uvicorn.Server):
    """uvicorn installs its own SIGINT/SIGTERM handlers; `tgdl serve` owns
    signal handling (graceful shutdown of workers first)."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


class WebBindError(OSError):
    pass


def bind_socket(host: str, port: int) -> socket.socket:
    """Bind ourselves: uvicorn would sys.exit() on failure, killing the loop."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError as exc:
        sock.close()
        raise WebBindError(f"{host}:{port}: {exc.strerror}") from exc
    return sock


async def start_web(core: "App") -> asyncio.Task[Any]:
    sock = bind_socket(core.cfg.web_host, core.cfg.web_port)
    config = uvicorn.Config(
        create_app(core), log_level="warning", access_log=False, lifespan="off",
        proxy_headers=False, server_header=False, timeout_graceful_shutdown=3,
    )
    server = _InLoopServer(config)
    core.web_server = server
    task = asyncio.create_task(server.serve(sockets=[sock]), name="web")
    for _ in range(100):
        if server.started or task.done():
            break
        await asyncio.sleep(0.05)
    if task.done():
        raise WebBindError(f"web server failed to start on {core.cfg.web_host}:{core.cfg.web_port}")
    log_event(log, logging.INFO, "web_start", f"http://{core.cfg.web_host}:{core.cfg.web_port}")
    return task
