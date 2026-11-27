"""Log viewer (application log and audit log)."""

from __future__ import annotations

import asyncio
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from ...logreader import LEVELS, read_log
from ..common import core, render, require_session
from ..pagination import get_pager, qs_with
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


def _page_query(request: Request, p: dict) -> dict[str, str]:
    """Filters plus pagination parameters, for pager links and live refresh."""
    q = {k: str(v) for k, v in p.items()}
    q["live"] = "1" if p["live"] else "0"
    for k in ("page", "per"):
        if request.query_params.get(k):
            q[k] = request.query_params[k]
    return q


@router.get("/logs")
async def logs_page(request: Request, _s: Session = Depends(require_session)) -> Response:
    p = _params(request)
    pager = get_pager(request, "logs")
    entries = pager.slice(await _entries(request, p))
    query = _page_query(request, p)
    return render(request, "logs.html", p=p, levels=LEVELS, line_choices=LINE_CHOICES,
                  entries=entries, pager=pager, query=query, live_qs=qs_with(query),
                  toggle_live_qs=_query(p, live=not p["live"]))


@router.get("/partials/logs")
async def logs_partial(request: Request, _s: Session = Depends(require_session)) -> Response:
    p = _params(request)
    pager = get_pager(request, "logs")
    entries = pager.slice(await _entries(request, p))
    return render(request, "partials/log_lines.html", entries=entries, pager=pager,
                  query=_page_query(request, p))
