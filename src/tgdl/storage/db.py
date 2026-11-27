"""SQLite connection management."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from ..config import ensure_private_file

SCHEMA_VERSION = 3
SCHEMA_FILE = Path(__file__).with_name("schema.sql")

# Upgrades for existing databases: (version reached, [(table, column, type)]).
# Columns are added only if missing, so a partial upgrade can be re-run.
MIGRATIONS: list[tuple[int, list[tuple[str, str, str]]]] = [
    (2, [("admin", "username", "TEXT")]),        # web login needs a username
    (3, [("monitors", "direction", "TEXT")]),    # received-only option
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    dt = dt or utcnow()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected")
        return self._conn

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Autocommit; multi-statement writes use transaction().
        self._conn = await aiosqlite.connect(self.path, isolation_level=None)
        try:
            self._conn.row_factory = aiosqlite.Row
            await self._conn.execute("PRAGMA journal_mode=WAL")
            await self._conn.execute("PRAGMA foreign_keys=ON")
            await self._conn.execute("PRAGMA busy_timeout=5000")
            await self._migrate()
        except BaseException:
            # aiosqlite runs a non-daemon thread per connection: without this
            # close, a failed start (e.g. migration error) hangs at exit.
            await self.close()
            raise
        self.fix_permissions()

    def fix_permissions(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            ensure_private_file(Path(str(self.path) + suffix))

    async def _migrate(self) -> None:
        async with self.conn.execute("PRAGMA user_version") as cur:
            row = await cur.fetchone()
        version = row[0] if row else 0
        if version == 0:
            # fresh database: schema.sql is always the latest version
            await self.conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
        else:
            for target, columns in MIGRATIONS:
                if version < target:
                    for table, column, ctype in columns:
                        await self._add_column(table, column, ctype)
            # tables introduced after the database was created
            await self.conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
        if version < SCHEMA_VERSION:
            await self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    async def _add_column(self, table: str, column: str, ctype: str) -> None:
        rows = await self.conn.execute_fetchall(f"PRAGMA table_info({table})")
        if not rows:  # table missing: created by schema.sql below with all columns
            return
        if column not in {r[1] for r in rows}:
            await self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ctype}")

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Serialize writers and commit/rollback atomically."""
        async with self._write_lock:
            try:
                await self.conn.execute("BEGIN IMMEDIATE")
                yield self.conn
            except BaseException:
                await self.conn.rollback()
                raise
            else:
                await self.conn.commit()

    async def execute(self, sql: str, params: tuple | dict = ()) -> int:
        """Run a single write statement (serialized with transactions, since
        all coroutines share one connection). Returns lastrowid."""
        async with self._write_lock:
            async with self.conn.execute(sql, params) as cur:
                return cur.lastrowid or 0

    async def execute_returning(self, sql: str, params: tuple | dict = ()) -> aiosqlite.Row | None:
        """Run a write statement with RETURNING; returns the first row."""
        async with self._write_lock:
            rows = await self.conn.execute_fetchall(sql, params)
        return next(iter(rows), None)

    # Reads run execute+fetch as ONE call on aiosqlite's thread. Splitting them
    # would let another coroutine modify the table between the two steps on
    # this shared connection, which SQLite does not define (seen as NULL
    # values in GROUP BY results).
    async def fetchone(self, sql: str, params: tuple | dict = ()) -> aiosqlite.Row | None:
        rows = await self.conn.execute_fetchall(sql, params)
        return next(iter(rows), None)

    async def fetchall(self, sql: str, params: tuple | dict = ()) -> list[aiosqlite.Row]:
        return list(await self.conn.execute_fetchall(sql, params))
