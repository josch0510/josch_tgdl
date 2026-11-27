"""Path template rendering and download directory validation."""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from string import Formatter

from .sanitize import sanitize_component, sanitize_filename

DEFAULT_PATH_TEMPLATE = (
    "{base_dir}/{chat_name}/{category}/{YYYY}/{YYYY-MM-DD}/{prefix}{msg_id}_{filename}"
)
TEMPLATE_VARS = frozenset({
    "base_dir", "chat_name", "chat_id", "category", "YYYY", "MM", "DD",
    "YYYY-MM-DD", "msg_id", "grouped_id", "prefix", "filename",
})
PREFIX_VARS = frozenset({"chat_name", "date", "msg_id", "category"})


class TemplateError(ValueError):
    """Invalid path/prefix template. ``code`` is an i18n key."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class PathNotAllowed(ValueError):
    """A directory or file path is outside the allowed base directories."""

    def __init__(self, code: str, path: str = "") -> None:
        super().__init__(f"{code}: {path}" if path else code)
        self.code = code
        self.path = path


Token = tuple[str, str | None]  # (literal, field) - exactly one is meaningful


def _parse(segment: str, allowed: frozenset[str]) -> list[Token]:
    try:
        parsed = list(Formatter().parse(segment))
    except ValueError as exc:
        raise TemplateError("template.syntax", str(exc)) from exc
    tokens: list[Token] = []
    for literal, field, spec, conv in parsed:
        if literal:
            tokens.append((literal, None))
        if field is None:
            continue
        if field == "" or spec or conv:
            raise TemplateError("template.bad_field", field)
        if field not in allowed:
            raise TemplateError("template.unknown_var", field)
        tokens.append(("", field))
    return tokens


def validate_path_template(template: str) -> list[list[Token]]:
    """Validate a path template; returns the parsed segments (excluding
    the leading ``{base_dir}``)."""
    if not template or "\\" in template or "\x00" in template:
        raise TemplateError("template.syntax")
    segments = template.split("/")
    if segments[0] != "{base_dir}":
        raise TemplateError("template.must_start_with_base_dir")
    if len(segments) < 2:
        raise TemplateError("template.needs_filename")
    parsed: list[list[Token]] = []
    for seg in segments[1:]:
        if seg.strip() in ("", ".", ".."):
            raise TemplateError("template.bad_segment", seg)
        tokens = _parse(seg, TEMPLATE_VARS)
        if any(f == "base_dir" for _, f in tokens):
            raise TemplateError("template.base_dir_position")
        parsed.append(tokens)
    if not any(f == "filename" for _, f in parsed[-1]):
        raise TemplateError("template.needs_filename")
    return parsed


def validate_prefix_template(template: str) -> list[Token]:
    if "\x00" in template:
        raise TemplateError("template.syntax")
    return _parse(template, PREFIX_VARS)


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def render_prefix(template: str, *, chat_name: str, local_date: datetime,
                  msg_id: int, category: str) -> str:
    tokens = validate_prefix_template(template)
    values = {
        "chat_name": sanitize_component(chat_name),
        "date": local_date.strftime("%Y-%m-%d"),
        "msg_id": str(msg_id),
        "category": sanitize_component(category),
    }
    out = "".join(lit if f is None else values[f] for lit, f in tokens)
    return sanitize_component(out, empty="") if out else ""


@dataclass(frozen=True)
class PathContext:
    base_dir: Path
    chat_name: str
    chat_id: int
    category: str
    msg_id: int
    filename: str
    message_date: datetime  # aware (UTC) or naive UTC
    tz: tzinfo
    grouped_id: int | None = None
    prefix_template: str = ""

    @property
    def local_date(self) -> datetime:
        return _as_utc(self.message_date).astimezone(self.tz)


def render_path(template: str, ctx: PathContext) -> Path:
    """Render ``template`` into an absolute path under ``ctx.base_dir``.

    Every substituted value is sanitized before substitution (so values can
    never introduce separators), and every resulting segment is sanitized
    again as a whole.
    """
    segments = validate_path_template(template)
    d = ctx.local_date
    prefix = render_prefix(ctx.prefix_template, chat_name=ctx.chat_name,
                           local_date=d, msg_id=ctx.msg_id, category=ctx.category)
    values = {
        "chat_name": sanitize_component(ctx.chat_name),
        "chat_id": str(ctx.chat_id),
        "category": sanitize_component(ctx.category),
        "YYYY": f"{d.year:04d}",
        "MM": f"{d.month:02d}",
        "DD": f"{d.day:02d}",
        "YYYY-MM-DD": d.strftime("%Y-%m-%d"),
        "msg_id": str(ctx.msg_id),
        "grouped_id": str(ctx.grouped_id) if ctx.grouped_id else "",
        "prefix": prefix,
        "filename": sanitize_filename(ctx.filename),
    }
    parts: list[str] = []
    last = len(segments) - 1
    for i, tokens in enumerate(segments):
        rendered = "".join(lit if f is None else values[f] for lit, f in tokens)
        parts.append(sanitize_filename(rendered) if i == last else sanitize_component(rendered))
    return Path(ctx.base_dir).joinpath(*parts)


def resolve_allowed(candidate: str | os.PathLike[str], allowed_roots: Sequence[Path]) -> Path:
    """Resolve ``candidate`` (following symlinks) and ensure it lies inside
    one of ``allowed_roots``. Returns the resolved path."""
    raw = os.fspath(candidate)
    if not raw or "\x00" in raw:
        raise PathNotAllowed("dir.invalid", raw)
    p = Path(raw).expanduser()
    if not p.is_absolute():
        raise PathNotAllowed("dir.not_absolute", raw)
    resolved = Path(os.path.realpath(p))
    for root in allowed_roots:
        r = Path(os.path.realpath(root))
        if resolved == r or resolved.is_relative_to(r):
            return resolved
    raise PathNotAllowed("dir.outside_allowed", raw)


def unique_path(path: Path) -> Path:
    """Return ``path`` or ``stem_N.ext`` if it (or its .part) already exists."""
    def taken(p: Path) -> bool:
        return p.exists() or p.with_name(p.name + ".part").exists()

    if not taken(path):
        return path
    stem, suffix = path.stem, path.suffix
    n = 1
    while True:
        cand = path.with_name(sanitize_filename(f"{stem}_{n}{suffix}"))
        if not taken(cand):
            return cand
        n += 1
