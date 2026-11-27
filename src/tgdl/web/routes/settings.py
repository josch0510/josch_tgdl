"""Global settings."""

from __future__ import annotations

from typing import Any
from zoneinfo import available_timezones

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from ...core.categories import CATEGORIES, MEDIA_KINDS
from ...i18n import SUPPORTED
from ...settings import MAX_CONCURRENCY, MIN_CONCURRENCY, SettingsError
from ..common import audit_web, core, redirect, render, require_csrf, require_session
from ..security import Session

router = APIRouter()

_TIMEZONES = sorted(available_timezones())
KIND_OPTIONS = [k for k in MEDIA_KINDS if k != "document"]


def _form_values(settings: dict[str, Any]) -> dict[str, Any]:
    cm = settings["category_map"]
    return {
        "default_dir": settings["default_dir"],
        "concurrency": settings["concurrency"],
        "path_template": settings["path_template"],
        "timezone": settings["timezone"],
        "default_media_types": settings["default_media_types"],
        "default_prefix": settings["default_prefix"],
        "language": settings["language"],
        "kinds": dict(cm["kinds"]),
        "extensions": {c: ", ".join(cm["extensions"].get(c, [])) for c in CATEGORIES},
        "mime": "\n".join(f"{k} = {v}" for k, v in cm["mime"].items()),
    }


def _ctx(request: Request, values: dict[str, Any],
         error: tuple[str, str, str] | None = None) -> dict[str, Any]:
    app = core(request)
    return {"v": values, "error": error, "kinds": MEDIA_KINDS, "kind_options": KIND_OPTIONS,
            "categories": CATEGORIES, "timezones": _TIMEZONES, "languages": SUPPORTED,
            "allowed_dirs": app.cfg.allowed_base_dirs, "min_c": MIN_CONCURRENCY,
            "max_c": MAX_CONCURRENCY}


def _parse_mime(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise SettingsError("category_map", "settings.bad_mime_line")
        prefix, cat = (p.strip() for p in line.split("=", 1))
        out[prefix] = cat
    return out


@router.get("/settings")
async def settings_page(request: Request, _s: Session = Depends(require_session)) -> Response:
    return render(request, "settings.html", **_ctx(request, _form_values(core(request).settings.all())))


@router.post("/settings")
async def settings_save(request: Request, _s: Session = Depends(require_csrf)) -> Response:
    app = core(request)
    form = await request.form()
    values = {
        "default_dir": str(form.get("default_dir", "")).strip(),
        "concurrency": str(form.get("concurrency", "")).strip(),
        "path_template": str(form.get("path_template", "")).strip(),
        "timezone": str(form.get("timezone", "")).strip(),
        "default_media_types": [k for k in form.getlist("default_media_types") if k in MEDIA_KINDS],
        "default_prefix": str(form.get("default_prefix", "")),
        "language": str(form.get("language", "")),
        "kinds": {k: str(form.get(f"kind_{k}", "")) for k in KIND_OPTIONS},
        "extensions": {c: str(form.get(f"ext_{c}", "")) for c in CATEGORIES},
        "mime": str(form.get("mime", "")),
    }
    try:
        category_map = {
            "kinds": values["kinds"],
            "extensions": {c: [e for e in (x.strip() for x in v.split(",")) if e]
                           for c, v in values["extensions"].items()},
            "mime": _parse_mime(values["mime"]),
        }
        diff = await app.settings.update({
            "default_dir": values["default_dir"],
            "concurrency": values["concurrency"],
            "path_template": values["path_template"],
            "timezone": values["timezone"],
            "default_media_types": values["default_media_types"],
            "default_prefix": values["default_prefix"],
            "language": values["language"],
            "category_map": category_map,
        })
    except SettingsError as exc:
        return render(request, "settings.html", status_code=400,
                      **_ctx(request, values, (exc.key, exc.code, exc.detail)))
    if diff:
        summary = {k: ("(changed)" if k == "category_map" else new) for k, (_, new) in diff.items()}
        audit_web(request, "settings_change", f"changes={summary}")
    return redirect("/settings", ok="settings_saved" if diff else "settings_unchanged")
