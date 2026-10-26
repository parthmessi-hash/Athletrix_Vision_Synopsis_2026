"""Local SQLite storage: the single source of truth for athletes, device status and sample histories.

Design
  - Python's built-in sqlite3, one database file whose path comes from settings (DATABASE_PATH).
  - One connection per process, shared across FastAPI's worker threads behind a lock. Every write
    runs in a short BEGIN IMMEDIATE ... COMMIT transaction, so an insert and its history trim
    succeed or fail together, and a successful POST means the transaction committed.
  - WAL journal mode with a busy timeout, so a reader never blocks on a writer for long.
  - Timestamps are stored as fixed-width UTC ISO-8601 text (`ts_utc`), so equivalent instants written
    with different offsets compare equal and ORDER BY / BETWEEN work lexically.
  - Each sample row also keeps the validated model as JSON (`payload`), so nulls, zeros and the
    original timestamp offset round-trip exactly.
Supported local setup: a single uvicorn process (the default; `--reload` is fine). The schema is
created automatically when the database is first opened.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS athletes (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    jersey_number INTEGER NOT NULL,
    sport         TEXT NOT NULL,
    team_id       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS device_status (
    device_id TEXT PRIMARY KEY,
    ts_utc    TEXT NOT NULL,
    payload   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS physiological_samples (
    athlete_id TEXT NOT NULL,
    ts_utc     TEXT NOT NULL,
    payload    TEXT NOT NULL,
    PRIMARY KEY (athlete_id, ts_utc)
);
CREATE TABLE IF NOT EXISTS tracking_samples (
    athlete_id TEXT NOT NULL,
    ts_utc     TEXT NOT NULL,
    payload    TEXT NOT NULL,
    PRIMARY KEY (athlete_id, ts_utc)
);
"""


def to_utc_iso(ts: datetime) -> str:
    """Normalise a timezone-aware datetime to fixed-width UTC text, e.g. 2026-10-25T03:23:45.728588+00:00."""
    return ts.astimezone(UTC).isoformat(timespec="microseconds")


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)
        self.closed = False

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Short exclusive write transaction. Commits on success, rolls back on any exception."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            yield self._conn

    def close(self) -> None:
        with self._lock:
            if not self.closed:
                self._conn.close()
                self.closed = True


# ---- process-wide current database ----------------------------------------
_current: Database | None = None
_current_lock = threading.Lock()


def get_database() -> Database:
    """The process's database, opened (and its schema created) on first use from settings."""
    global _current
    if _current is None or _current.closed:
        with _current_lock:
            if _current is None or _current.closed:
                _current = Database(get_settings().database_path)
    return _current


def current_database() -> Database | None:
    """The database currently in use, or None when nothing has been opened or set. Never opens one."""
    return _current


def set_database(db: Database | None) -> None:
    """Point the stores at a specific database (tests use isolated temporary files)."""
    global _current
    _current = db
