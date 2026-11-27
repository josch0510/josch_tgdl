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
