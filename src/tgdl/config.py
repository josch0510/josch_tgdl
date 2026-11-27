"""Static configuration from .env (secrets, bind address, allowed roots)."""

from __future__ import annotations

import logging
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

log = logging.getLogger("tgdl.config")


class ConfigError(Exception):
    """Configuration problem. ``code`` is an i18n key."""

    def __init__(self, code: str, **params: object) -> None:
        super().__init__(code)
        self.code = code
        self.params = params


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        path.chmod(0o700)


def ensure_private_file(path: Path) -> bool:
    """chmod 600 if group/other have any access. Returns True if changed."""
    if path.exists() and stat.S_IMODE(path.stat().st_mode) & 0o077:
        path.chmod(0o600)
        return True
    return False


def _bool(v: str | None, default: bool = False) -> bool:
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class AppConfig:
    home: Path
    api_id: int | None
    api_hash: str | None = field(repr=False)
    allowed_base_dirs: tuple[Path, ...]
    web_host: str = "127.0.0.1"
    web_port: int = 8080
    cookie_secure: bool = False
    extra_hosts: tuple[str, ...] = ()

    @property
    def allowed_hosts(self) -> list[str]:
        """Host header values the web UI answers to (DNS-rebinding guard)."""
        hosts = {"localhost", "127.0.0.1", "::1", "[::1]", *self.extra_hosts}
        if self.web_host not in ("0.0.0.0", "::", ""):
            hosts.add(self.web_host)
        return sorted(hosts)

    @property
    def env_path(self) -> Path:
        return self.home / ".env"

    @property
    def data_dir(self) -> Path:
        return self.home / "data"

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def session_path(self) -> Path:
        # Telethon appends ".session"
        return self.data_dir / "tgdl"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "tgdl.db"

    def require_api(self) -> tuple[int, str]:
        if not self.api_id or not self.api_hash:
            raise ConfigError("config.missing_api")
        return self.api_id, self.api_hash

    def prepare_dirs(self) -> None:
        ensure_private_dir(self.data_dir)
        ensure_private_dir(self.logs_dir)


def load_config(home: Path | None = None) -> AppConfig:
    home = (home or Path(os.environ.get("TGDL_HOME") or Path.cwd())).resolve()
    env_file = home / ".env"
    values: dict[str, str | None] = {}
    if env_file.exists():
        if ensure_private_file(env_file):
            log.warning("Fixed permissions of .env to 600")
        values = dict(dotenv_values(env_file))
    # Real environment variables override .env
    for k in ("API_ID", "API_HASH", "ALLOWED_BASE_DIRS", "WEB_HOST", "WEB_PORT", "COOKIE_SECURE",
              "ALLOWED_HOSTS"):
        if os.environ.get(k):
            values[k] = os.environ[k]

    api_id_raw = (values.get("API_ID") or "").strip()
    try:
        api_id = int(api_id_raw) if api_id_raw else None
    except ValueError as exc:
        raise ConfigError("config.bad_api_id") from exc

    roots_raw = (values.get("ALLOWED_BASE_DIRS") or "").strip()
    roots: list[Path] = []
    for part in roots_raw.split(os.pathsep):
        part = part.strip()
        if not part:
            continue
        p = Path(part).expanduser()
        if not p.is_absolute():
            raise ConfigError("config.base_dir_not_absolute", path=part)
        roots.append(Path(os.path.realpath(p)))
    if not roots:
        roots = [Path(os.path.realpath(Path.home() / "tgdl-downloads"))]

    try:
        port = int(values.get("WEB_PORT") or 8080)
    except ValueError as exc:
        raise ConfigError("config.bad_port") from exc

    return AppConfig(
        home=home,
        api_id=api_id,
        api_hash=(values.get("API_HASH") or "").strip() or None,
        allowed_base_dirs=tuple(roots),
        web_host=(values.get("WEB_HOST") or "127.0.0.1").strip(),
        web_port=port,
        cookie_secure=_bool(values.get("COOKIE_SECURE")),
        extra_hosts=tuple(h.strip().lower() for h in (values.get("ALLOWED_HOSTS") or "").split(",")
                          if h.strip()),
    )
