"""Chat monitors: browse joined chats, add/remove/toggle, per-chat
settings, backfill."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from ...core.categories import MEDIA_KINDS
from ...core.filters import DIRECTIONS
from ...core.paths import PathNotAllowed, TemplateError, resolve_allowed, validate_prefix_template
from ...downloader.flood import FLOOD_ERRORS
from ...storage.db import parse_iso
from ...telegram.backfill import BackfillError
from ...telegram.client import DialogCache
from ..common import (
    audit_web, core, current_url, redirect, render, require_csrf, require_session,
)
from ..monitor_view import describe
from ..pagination import get_pager
from ..security import Session

router = APIRouter()
log = logging.getLogger("tgdl.web")

MB = 1024 * 1024
MAX_KEYWORD = 200


async def _monitor_or_404(request: Request, chat_id: int) -> dict[str, Any]:
    m = await core(request).repo.get_monitor(chat_id)
    if m is None:
        raise HTTPException(status_code=404)
    return m


@router.get("/monitors")
async def monitors_page(request: Request, _s: Session = Depends(require_session)) -> Response:
    app = core(request)
    mon_pager = get_pager(request, "monitors", "m_")
    monitors = mon_pager.slice(await app.repo.list_monitors())
    views = {m["chat_id"]: describe(app, m) for m in monitors}
    running = {m["chat_id"]: app.backfill.running_for(m["chat_id"]) for m in monitors}
    bf_pager = get_pager(request, "backfills", "b_").set_total(await app.repo.count_backfills())
    backfills = await app.repo.list_backfills(bf_pager.per_page, bf_pager.offset)
    for b in backfills:
        # date_to is stored as the exclusive end (next local midnight)
        b["date_to_incl"] = parse_iso(b["date_to"]) - timedelta(seconds=1)
    today = datetime.now(app.settings.tz).date().isoformat()
    back = current_url(request)
    return render(request, "monitors.html", monitors=monitors, running=running,
                  backfills=backfills, live_backfill=app.backfill.progress, today=today,
                  mon_pager=mon_pager, bf_pager=bf_pager, back=back, views=views)


def _back(form: Any) -> str:
    """Return to the page the action was made from (pagination kept)."""
    target = str(form.get("back", "/monitors"))
    return target if target.startswith("/monitors") and not target.startswith("//") else "/monitors"


@router.get("/monitors/dialogs")
async def dialogs_partial(request: Request, q: str = "", type: str = "", refresh: int = 0,
                          _s: Session = Depends(require_session)) -> Response:
    app = core(request)
    error = None
    items: list = []
    try:
        items = DialogCache.search(await app.dialogs.get(refresh=bool(refresh)), q[:100], type)
    except FLOOD_ERRORS:
        error = "monitors.dialogs_flood"
    except Exception:  # noqa: BLE001 - e.g. disconnected
        log.exception("Listing dialogs failed")
        error = "monitors.dialogs_failed"
    monitored = set(app.monitors.monitors)
    pager = get_pager(request, "dialogs")
    query = {k: v for k, v in request.query_params.items() if k != "refresh"}
    return render(request, "partials/dialog_list.html", items=pager.slice(items), pager=pager,
                  monitored=monitored, error=error, query=query)


@router.post("/monitors/add")
async def add_monitor(request: Request, _s: Session = Depends(require_csrf)) -> Response:
    app = core(request)
    form = await request.form()
    try:
        chat_id = int(str(form.get("chat_id", "")))
    except ValueError:
        return redirect("/monitors", err="bad_request")
    chat = app.dialogs.find(chat_id)
    if chat is None:  # only chats from the user's own dialog list
        return redirect("/monitors", err="chat_not_found")
    await app.monitors.add(chat)
    audit_web(request, "monitor_add", f"chat={chat.chat_id} type={chat.chat_type}")
    return redirect("/monitors", ok="monitor_added")


@router.post("/monitors/{chat_id}/toggle")
async def toggle_monitor(request: Request, chat_id: int,
                         _s: Session = Depends(require_csrf)) -> Response:
    m = await _monitor_or_404(request, chat_id)
    enabled = not m["enabled"]
    await core(request).monitors.set_enabled(chat_id, enabled)
    audit_web(request, "monitor_enable" if enabled else "monitor_disable", f"chat={chat_id}")
    return redirect(_back(await request.form()), ok="monitor_enabled" if enabled else "monitor_disabled")


@router.post("/monitors/{chat_id}/delete")
async def delete_monitor(request: Request, chat_id: int,
                         _s: Session = Depends(require_csrf)) -> Response:
    await _monitor_or_404(request, chat_id)
    app = core(request)
    task_id = app.backfill.running_for(chat_id)
    if task_id is not None:
        await app.backfill.cancel(task_id)
    await app.monitors.remove(chat_id)
    audit_web(request, "monitor_remove", f"chat={chat_id}")
    return redirect(_back(await request.form()), ok="monitor_removed")


# ------------------------------------------------------ per-chat settings
def _settings_ctx(request: Request, m: dict[str, Any], values: dict[str, Any],
                  error: str | None = None) -> dict[str, Any]:
    app = core(request)
    return {"m": m, "v": values, "error": error, "kinds": MEDIA_KINDS,
            "defaults": app.settings.all(), "allowed_dirs": app.cfg.allowed_base_dirs}


def _values_from_monitor(m: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    return {
        "use_default_types": m["media_types"] is None,
        # pre-filled with the defaults, so customizing starts from the current behaviour
        "media_types": m["media_types"] if m["media_types"] is not None
        else list(defaults["default_media_types"]),
        "download_dir": m["download_dir"] or "",
        "filename_prefix": m["filename_prefix"] if m["filename_prefix"] is not None else "",
        "use_default_prefix": m["filename_prefix"] is None,
        "min_mb": "" if m["min_size"] is None else f"{m['min_size'] / MB:g}",
        "max_mb": "" if m["max_size"] is None else f"{m['max_size'] / MB:g}",
        "keyword": m["keyword"] or "",
        "direction": m.get("direction") or "",   # "" = global default
    }


def _parse_mb(raw: str) -> int | None:
    raw = raw.strip()
    if not raw:
        return None
    value = float(raw)
    if value < 0 or value > 10_000_000:
        raise ValueError
    return int(value * MB)


@router.get("/monitors/{chat_id}/settings")
async def monitor_settings(request: Request, chat_id: int,
                           _s: Session = Depends(require_session)) -> Response:
    m = await _monitor_or_404(request, chat_id)
    return render(request, "monitor_settings.html", **_settings_ctx(request, m, _values_from_monitor(m, core(request).settings.all())))


@router.post("/monitors/{chat_id}/settings")
async def monitor_settings_save(request: Request, chat_id: int,
                                _s: Session = Depends(require_csrf)) -> Response:
    app = core(request)
    m = await _monitor_or_404(request, chat_id)
    form = await request.form()
    values = {
        "use_default_types": form.get("use_default_types") == "1",
        "media_types": [k for k in form.getlist("media_types") if k in MEDIA_KINDS],
        "download_dir": str(form.get("download_dir", "")).strip(),
        "filename_prefix": str(form.get("filename_prefix", "")),
        "use_default_prefix": form.get("use_default_prefix") == "1",
        "min_mb": str(form.get("min_mb", "")),
        "max_mb": str(form.get("max_mb", "")),
        "keyword": str(form.get("keyword", "")).strip()[:MAX_KEYWORD],
        "direction": str(form.get("direction", "")),
    }

    def fail(code: str) -> Response:
        return render(request, "monitor_settings.html", status_code=400,
                      **_settings_ctx(request, m, values, code))

    download_dir: str | None = None
    if values["download_dir"]:
        try:
            download_dir = str(resolve_allowed(values["download_dir"], app.cfg.allowed_base_dirs))
        except PathNotAllowed as exc:
            return fail(exc.code)
    prefix: str | None = None
    if not values["use_default_prefix"]:
        try:
            validate_prefix_template(values["filename_prefix"])
        except TemplateError as exc:
            return fail(exc.code)
        prefix = values["filename_prefix"]
    try:
        min_size, max_size = _parse_mb(values["min_mb"]), _parse_mb(values["max_mb"])
    except ValueError:
        return fail("monitor.bad_size")
    if min_size is not None and max_size is not None and min_size > max_size:
        return fail("monitor.min_gt_max")
    media_types = None if values["use_default_types"] else values["media_types"]
    if media_types is not None and not media_types:
        return fail("monitor.no_types")

    if values["direction"] not in ("", *DIRECTIONS):
        return fail("settings.invalid")
    changes = {"media_types": media_types, "download_dir": download_dir,
               "filename_prefix": prefix, "min_size": min_size, "max_size": max_size,
               "keyword": values["keyword"] or None, "direction": values["direction"] or None}
    await app.repo.update_monitor(chat_id, **changes)
    await app.monitors.reload(chat_id)
    diff = {k: v for k, v in changes.items() if m.get(k) != v}
    audit_web(request, "monitor_settings", f"chat={chat_id} changes={diff}")
    return redirect("/monitors", ok="monitor_saved")


# --------------------------------------------------------------- backfill
@router.post("/monitors/{chat_id}/backfill")
async def start_backfill(request: Request, chat_id: int,
                         _s: Session = Depends(require_csrf)) -> Response:
    await _monitor_or_404(request, chat_id)
    form = await request.form()
    try:
        d_from = date.fromisoformat(str(form.get("date_from", "")))
        d_to = date.fromisoformat(str(form.get("date_to", "")))
    except ValueError:
        return redirect(_back(form), err="bad_dates")
    try:
        await core(request).backfill.start(chat_id, d_from, d_to)
    except BackfillError as exc:
        return redirect(_back(form), err=exc.code.split(".", 1)[1])
    audit_web(request, "backfill_start", f"chat={chat_id} from={d_from} to={d_to}")
    return redirect(_back(form), ok="backfill_started")


@router.post("/backfill/{task_id}/cancel")
async def cancel_backfill(request: Request, task_id: int,
                          _s: Session = Depends(require_csrf)) -> Response:
    back = _back(await request.form())
    if await core(request).backfill.cancel(task_id):
        audit_web(request, "backfill_cancel", f"task={task_id}")
        return redirect(back, ok="backfill_cancelled")
    return redirect(back, err="backfill_not_running")
