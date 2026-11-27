"""Download history: search/filter, retry failed jobs, re-download
records whose file is missing."""

from __future__ import annotations

import asyncio
import os
from datetime import date
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from ...core.categories import CATEGORIES
from ...storage.repo import DuplicateDownload, HistoryFilter
from ...telegram.backfill import BackfillError, local_range_to_utc
from ..common import audit_web, core, redirect, render, require_csrf, require_session
from ..security import Session

router = APIRouter()

PAGE_SIZE = 50


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None


def _date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _filters(request: Request) -> tuple[dict[str, Any], HistoryFilter, str | None]:
    qp = request.query_params
    raw = {
        "tab": qp.get("tab") if qp.get("tab") in ("done", "failed") else "done",
        "chat": qp.get("chat", ""),
        "category": qp.get("category") if qp.get("category") in CATEGORIES else "",
        "q": qp.get("q", "")[:200],
        "date_from": qp.get("date_from", ""),
        "date_to": qp.get("date_to", ""),
    }
    d_from, d_to = _date(raw["date_from"]), _date(raw["date_to"])
    since = until = None
    error = None
    tz = core(request).settings.tz
    try:
        if d_from or d_to:
            since, until = local_range_to_utc(d_from or date(1970, 1, 1), d_to or date(9999, 12, 30), tz)
            since = since if d_from else None
            until = until if d_to else None
    except BackfillError:
        error = "history.bad_range"
    f = HistoryFilter(chat_id=_int(raw["chat"]), category=raw["category"] or None,
                      query=raw["q"].strip(), since=since, until=until)
    return raw, f, error


@router.get("/history")
async def history_page(request: Request, _s: Session = Depends(require_session)) -> Response:
    app = core(request)
    raw, f, error = _filters(request)
    page = max(1, _int(request.query_params.get("page")) or 1)
    offset = (page - 1) * PAGE_SIZE
    if raw["tab"] == "failed":
        rows, total = await app.repo.search_failed_jobs(f, limit=PAGE_SIZE, offset=offset)
    else:
        rows, total = await app.repo.search_downloads(f, limit=PAGE_SIZE, offset=offset)
        exists = await asyncio.to_thread(lambda: [os.path.exists(r["saved_path"]) for r in rows])
        for r, e in zip(rows, exists, strict=True):
            r["exists"] = e
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    base_qs = {k: v for k, v in raw.items() if v}
    return render(
        request, "history.html", raw=raw, rows=rows, total=total, page=page, pages=pages,
        error=error, categories=CATEGORIES, chats=await app.repo.history_chats(),
        failed_count=(await app.repo.job_counts()).get("failed", 0),
        qs_prev=urlencode({**base_qs, "page": page - 1}),
        qs_next=urlencode({**base_qs, "page": page + 1}),
        tab_qs={t: urlencode({**base_qs, "tab": t}) for t in ("done", "failed")},
        back=request.url.path + ("?" + request.url.query if request.url.query else ""),
    )


def _back(form: Any) -> str:
    target = str(form.get("back", "/history"))
    return target if target.startswith("/history") else "/history"


@router.post("/history/retry/{job_id}")
async def retry_one(request: Request, job_id: int, _s: Session = Depends(require_csrf)) -> Response:
    app = core(request)
    result = await app.repo.retry_job(job_id)
    form = await request.form()
    if result == "queued":
        await app.queue.notify()
        audit_web(request, "job_retry", f"job={job_id}")
        return redirect(_back(form), ok="job_requeued")
    return redirect(_back(form), err="job_" + result)


@router.post("/history/retry-failed")
async def retry_all(request: Request, _s: Session = Depends(require_csrf)) -> Response:
    app = core(request)
    queued = 0
    for job_id in await app.repo.failed_job_ids():
        if await app.repo.retry_job(job_id) == "queued":
            queued += 1
    await app.queue.notify()
    audit_web(request, "job_retry_all", f"queued={queued}")
    return redirect(_back(await request.form()), ok="jobs_requeued", n=str(queued))


@router.post("/history/redownload/{download_id}")
async def redownload_one(request: Request, download_id: int,
                         _s: Session = Depends(require_csrf)) -> Response:
    app = core(request)
    form = await request.form()
    d = await app.repo.get_download(download_id)
    if d is None:
        return redirect(_back(form), err="bad_request")
    if await asyncio.to_thread(os.path.exists, d["saved_path"]):
        return redirect(_back(form), err="file_exists")
    try:
        job_id = await app.repo.requeue_download(download_id)
    except DuplicateDownload:
        return redirect(_back(form), err="job_skipped")
    await app.queue.notify()
    audit_web(request, "redownload", f"download={download_id} job={job_id} chat={d['chat_id']} "
                                     f"msg={d['message_id']}")
    return redirect(_back(form), ok="redownload_queued")


@router.post("/history/redownload-missing")
async def redownload_missing(request: Request, _s: Session = Depends(require_csrf)) -> Response:
    app = core(request)
    paths = await app.repo.download_paths()
    missing = await asyncio.to_thread(lambda: [i for i, p in paths if not os.path.exists(p)])
    queued = 0
    for download_id in missing:
        try:
            if await app.repo.requeue_download(download_id) is not None:
                queued += 1
        except DuplicateDownload:
            pass
    await app.queue.notify()
    audit_web(request, "redownload_missing", f"missing={len(missing)} queued={queued}")
    return redirect(_back(await request.form()), ok="missing_requeued", n=str(queued))
