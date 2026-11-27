"""Shared pagination for every list in the web UI.

Each list has a key (used for the per-browser "rows per page" cookie) and a
query-parameter prefix, so several paginated lists can live on one page.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar
from urllib.parse import urlencode

from fastapi import Request

PER_PAGE_CHOICES: tuple[int, ...] = (10, 25, 50)
DEFAULT_PER_PAGE = 25
COOKIE_PREFIX = "tgdl_per_"

T = TypeVar("T")


@dataclass
class Pager:
    key: str                 # cookie name suffix, e.g. "history"
    page: int = 1
    per_page: int = DEFAULT_PER_PAGE
    prefix: str = ""         # query parameter prefix, e.g. "m_"
    total: int = 0
    explicit_per: bool = False

    @property
    def page_param(self) -> str:
        return f"{self.prefix}page"

    @property
    def per_param(self) -> str:
        return f"{self.prefix}per"

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.per_page))

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.per_page

    @property
    def first_item(self) -> int:
        return 0 if self.total == 0 else self.offset + 1

    @property
    def last_item(self) -> int:
        return min(self.total, self.offset + self.per_page)

    @property
    def choices(self) -> tuple[int, ...]:
        return PER_PAGE_CHOICES

    def set_total(self, total: int) -> "Pager":
        """Record the total and clamp the page (e.g. after items were removed)."""
        self.total = max(0, int(total))
        self.page = min(max(1, self.page), self.pages)
        return self

    def slice(self, items: Sequence[T]) -> list[T]:
        self.set_total(len(items))
        return list(items[self.offset:self.offset + self.per_page])

    def window(self, radius: int = 2) -> list[int | None]:
        """Page numbers to show; None marks a gap (rendered as …)."""
        last = self.pages
        wanted = {1, last, *range(self.page - radius, self.page + radius + 1)}
        out: list[int | None] = []
        prev = 0
        for n in sorted(p for p in wanted if 1 <= p <= last):
            if n - prev > 1:
                out.append(None)
            out.append(n)
            prev = n
        return out


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None


def get_pager(request: Request, key: str, prefix: str = "") -> Pager:
    qp = request.query_params
    per = _int(qp.get(f"{prefix}per"))
    explicit = per in PER_PAGE_CHOICES
    if not explicit:
        per = _int(request.cookies.get(COOKIE_PREFIX + key))
    if per not in PER_PAGE_CHOICES:
        per = DEFAULT_PER_PAGE
    page = _int(qp.get(f"{prefix}page")) or 1
    return Pager(key=key, page=max(1, page), per_page=int(per), prefix=prefix, explicit_per=explicit)


def remember_per_page(response: Any, pagers: list[Pager], *, secure: bool) -> None:
    """Persist explicitly chosen rows-per-page values in the browser."""
    for p in pagers:
        if p.explicit_per:
            response.set_cookie(COOKIE_PREFIX + p.key, str(p.per_page), max_age=365 * 86400,
                                httponly=True, samesite="lax", secure=secure, path="/")


def qs_with(params: Mapping[str, Any], updates: Mapping[str, Any] | None = None,
            drop: Sequence[str] = ()) -> str:
    """Current query parameters with updates applied (None removes a key)."""
    merged = {k: v for k, v in params.items() if k not in drop}
    for k, v in (updates or {}).items():
        if v is None:
            merged.pop(k, None)
        else:
            merged[k] = v
    return urlencode({k: v for k, v in merged.items() if v not in ("", None)})
