"""Log viewer (application log and audit log)."""

from __future__ import annotations

import asyncio
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from ...logreader import LEVELS, read_log
from ..common import core, render, require_session
from ..security import Session

router = APIRouter()

SOURCES = {"app": "tgdl.log", "audit": "audit.log"}
LINE_CHOICES = (200, 500, 1000, 5000)


def _params(request: Request) -> dict[str, str | int | bool]:
    qp = request.query_params
    source = qp.get("source", "app")
    level = qp.get("level", "INFO")
    try:
        lines = int(qp.get("lines", "500"))
    except ValueError:
        lines = 500
    return {
        "source": source if source in SOURCES else "app",
        "level": level if level in LEVELS else "INFO",
        "lines": lines if lines in LINE_CHOICES else 500,
        "q": qp.get("q", "")[:200],
        "live": qp.get("live", "1") == "1",
    }


async def _entries(request: Request, p: dict) -> list:
    path = core(request).cfg.logs_dir / SOURCES[str(p["source"])]
    entries = await asyncio.to_thread(read_log, path, lines=int(p["lines"]),
                                      min_level=str(p["level"]), query=str(p["q"]))
    return list(reversed(entries))  # newest first


def _query(p: dict, **override: object) -> str:
    merged = {**p, **override}
    merged["live"] = "1" if merged["live"] else "0"
    return urlencode(merged)


@router.get("/logs")
async def logs_page(request: Request, _s: Session = Depends(require_session)) -> Response:
    p = _params(request)
    return render(request, "logs.html", p=p, levels=LEVELS, line_choices=LINE_CHOICES,
                  entries=await _entries(request, p), qs=_query(p),
                  toggle_live_qs=_query(p, live=not p["live"]))


@router.get("/partials/logs")
async def logs_partial(request: Request, _s: Session = Depends(require_session)) -> Response:
    p = _params(request)
    return render(request, "partials/log_lines.html", entries=await _entries(request, p))
