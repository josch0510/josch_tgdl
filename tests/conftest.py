from __future__ import annotations

from pathlib import Path

import pytest

from tgdl.config import AppConfig
from tgdl.settings import SettingsService
from tgdl.storage.db import Database
from tgdl.storage.repo import Repository


@pytest.fixture
def base_dir(tmp_path: Path) -> Path:
    d = tmp_path / "downloads"
    d.mkdir()
    return d


@pytest.fixture
def cfg(tmp_path: Path, base_dir: Path) -> AppConfig:
    return AppConfig(home=tmp_path, api_id=1, api_hash="x" * 32,
                     allowed_base_dirs=(base_dir.resolve(),))


@pytest.fixture
async def db(tmp_path: Path):
    database = Database(tmp_path / "data" / "test.db")
    await database.connect()
    yield database
    await database.close()


@pytest.fixture
def repo(db: Database) -> Repository:
    return Repository(db)


@pytest.fixture
async def settings(cfg: AppConfig, repo: Repository) -> SettingsService:
    s = SettingsService(cfg, repo)
    await s.load()
    return s


@pytest.fixture
def fast(monkeypatch):
    """Shrink timers so worker/backfill tests run quickly."""
    from tgdl.downloader import flood, queue, worker
    from tgdl.telegram import monitor
    monkeypatch.setattr(queue, "POLL_INTERVAL", 0.05)
    monkeypatch.setattr(worker, "BACKOFF_BASE", 0.05)
    monkeypatch.setattr(flood, "SAFETY_MARGIN", 0.0)
    monkeypatch.setattr(monitor, "ALBUM_SWEEP_DELAY", 0.01)


@pytest.fixture
async def app(cfg, db, repo, settings, fast):
    from fakes import FakeTelegram
    from tgdl.app import App
    a = App.build(cfg, FakeTelegram(), db, repo, settings)
    yield a
    a.queue.accepting = False
    await a.monitors.detach()
    await a.backfill.stop_all()
    await a.pool.stop()
