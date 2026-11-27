"""Dashboard + Server-Sent Events stream."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from datetime import datetime
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response, StreamingResponse

from ..common import core, render, require_session
from ..monitor_view import describe
from ..pagination import Pager, get_pager
from ..security import SESSION_COOKIE, Session, SessionManager

if TYPE_CHECKING:
    from ...app import App

router = APIRouter()

SSE_PING_SECONDS = 15
SSE_RECHECK_SESSION_SECONDS = 60
# Event types the browser cares about (others stay server-side).
SSE_TYPES = {"progress", "job_start", "job_end", "queue", "flood", "pool", "backfill", "monitors",
             "log"}


async def dashboard_data(app: "App", pager: Pager, mon_pager: Pager) -> dict[str, Any]:
    tz = app.settings.tz
    today_start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    today_count, today_bytes = await app.repo.download_stats_since(today_start)
    counts = await app.repo.job_counts()
    pool = app.pool.status()
    for a in pool["active"]:
        a["percent"] = int(a["done"] * 100 / a["total"]) if a["total"] else 0
        a["eta"] = (a["total"] - a["done"]) / a["speed"] if a["speed"] and a["total"] else None
    return {
        "today_count": today_count,
        "today_bytes": today_bytes,
        "pending": counts.get("pending", 0),
        "running": counts.get("running", 0),
        "failed": counts.get("failed", 0),
        "pool": pool,
        "per_chat": await app.repo.per_chat_stats(
            today_start, limit=pager.per_page,
            offset=pager.set_total(await app.repo.count_download_chats()).offset),
        "per_chat_pager": pager,
        "backfills": [p for p in app.backfill.progress.values() if p["status"] == "running"],
        "monitor_views": [describe(app, m) for m in mon_pager.slice(
            sorted(app.monitors.monitors.values(),
                   key=lambda m: (not m["enabled"], m["chat_title"].casefold())))],
        "monitor_pager": mon_pager,
        "monitors_enabled": app.monitors.enabled_count,
        "monitors_total": len(app.monitors.monitors),
        "floods": app.flood_status(),
        "now": time.time(),
    }


@router.get("/")
async def dashboard(request: Request, _s: Session = Depends(require_session)) -> Response:
    pager = get_pager(request, "perchat", "c_")
    mon_pager = get_pager(request, "dashmonitors", "w_")
    return render(request, "dashboard.html", d=await dashboard_data(core(request), pager, mon_pager),
                  pager=pager, mon_pager=mon_pager)


@router.get("/partials/dashboard")
async def dashboard_partial(request: Request, _s: Session = Depends(require_session)) -> Response:
    pager = get_pager(request, "perchat", "c_")
    mon_pager = get_pager(request, "dashmonitors", "w_")
    return render(request, "partials/dashboard_live.html",
                  d=await dashboard_data(core(request), pager, mon_pager),
                  pager=pager, mon_pager=mon_pager, query=dict(request.query_params))


@router.get("/partials/flood")
async def flood_partial(request: Request, _s: Session = Depends(require_session)) -> Response:
    return render(request, "partials/flood.html")


@router.get("/events")
async def events(request: Request, _s: Session = Depends(require_session)) -> Response:
    app = core(request)
    sessions: SessionManager = request.app.state.sessions
    token = request.cookies.get(SESSION_COOKIE)

    async def stream() -> AsyncIterator[str]:
        yield "retry: 3000\n\n"
        last_ping = last_check = time.monotonic()
        async with app.bus.subscribe() as q:
            while not app.shutting_down.is_set():
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=1.0)
                except TimeoutError:
                    ev = None
                now = time.monotonic()
                if now - last_check >= SSE_RECHECK_SESSION_SECONDS:
                    last_check = now
                    if await sessions.validate(token) is None:
                        yield "event: logout\ndata: {}\n\n"
                        return
                if await request.is_disconnected():
                    return
                if ev is not None and ev.type in SSE_TYPES:
                    yield f"event: {ev.type}\ndata: {json.dumps(ev.data, default=str)}\n\n"
                elif now - last_ping >= SSE_PING_SECONDS:
                    last_ping = now
                    yield ": ping\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

