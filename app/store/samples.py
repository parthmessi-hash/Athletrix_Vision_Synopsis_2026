"""SQLite-backed per-athlete sample store: bounded, timestamp-ordered history whose newest row is the "latest".

One table per stream. Latest-sample reads and history reads come from the same rows, so they can
never disagree. Within a single transaction an insert replaces any row with the same UTC instant
(equal-timestamp => most recent arrival wins) and then trims the oldest rows beyond the cap.
"""

from __future__ import annotations

from datetime import datetime
from typing import NamedTuple
from typing import Generic, Protocol, TypeVar

from pydantic import BaseModel

from app.db import Database, get_database, to_utc_iso
from app.store.history import MAX_HISTORY_SAMPLES


class _AthleteSample(Protocol):
    athlete_id: str
    timestamp: datetime


S = TypeVar("S", bound=BaseModel)


class Retention(NamedTuple):
    """Count and recorded UTC time bounds of the rows retained for one athlete in one stream."""

    count: int
    earliest: datetime | None
    latest: datetime | None


class SampleStore(Generic[S]):
    def __init__(self, table: str, model: type[S], max_samples: int = MAX_HISTORY_SAMPLES, db: Database | None = None) -> None:
        if table not in ("physiological_samples", "tracking_samples"):
            raise ValueError(f"unknown sample table {table!r}")
        self._table = table
        self._model = model
        self._max = max_samples
        self._db = db

    @property
    def db(self) -> Database:
        return self._db or get_database()

    def _row(self, row) -> S:
        return self._model.model_validate_json(row["payload"])

    # ---- writes -----------------------------------------------------------
    def add(self, sample: S) -> None:
        """Insert (or replace an equal-instant row) and trim to the cap, atomically."""
        athlete_id = sample.athlete_id  # type: ignore[attr-defined]
        ts = to_utc_iso(sample.timestamp)  # type: ignore[attr-defined]
        t = self._table
        with self.db.transaction() as c:
            c.execute(
                f"INSERT INTO {t} (athlete_id, ts_utc, payload) VALUES (?, ?, ?) "
                f"ON CONFLICT(athlete_id, ts_utc) DO UPDATE SET payload = excluded.payload",
                (athlete_id, ts, sample.model_dump_json()),
            )
            c.execute(
                f"DELETE FROM {t} WHERE athlete_id = ? AND rowid IN "
                f"(SELECT rowid FROM {t} WHERE athlete_id = ? ORDER BY ts_utc DESC LIMIT -1 OFFSET ?)",
                (athlete_id, athlete_id, self._max),
            )

    def upsert(self, sample: S) -> S:
        """Record the sample; return whichever sample is now the athlete's latest."""
        self.add(sample)
        latest = self.get(sample.athlete_id)  # type: ignore[attr-defined]
        assert latest is not None
        return latest

    # ---- reads ------------------------------------------------------------
    def get(self, athlete_id: str) -> S | None:
        """Newest sample by timestamp, or None."""
        with self.db.read() as c:
            row = c.execute(f"SELECT payload FROM {self._table} WHERE athlete_id = ? ORDER BY ts_utc DESC LIMIT 1", (athlete_id,)).fetchone()
        return self._row(row) if row else None

    latest = get

    def recent(self, athlete_id: str, limit: int) -> list[S]:
        """The newest `limit` samples, oldest to newest."""
        with self.db.read() as c:
            rows = c.execute(f"SELECT payload FROM {self._table} WHERE athlete_id = ? ORDER BY ts_utc DESC LIMIT ?", (athlete_id, limit)).fetchall()
        return [self._row(r) for r in reversed(rows)]

    def between(self, athlete_id: str, start: datetime, end: datetime) -> list[S]:
        """Samples with start <= timestamp <= end (compared in UTC), oldest to newest."""
        with self.db.read() as c:
            rows = c.execute(
                f"SELECT payload FROM {self._table} WHERE athlete_id = ? AND ts_utc BETWEEN ? AND ? ORDER BY ts_utc ASC",
                (athlete_id, to_utc_iso(start), to_utc_iso(end)),
            ).fetchall()
        return [self._row(r) for r in rows]

    def count(self, athlete_id: str) -> int:
        with self.db.read() as c:
            return c.execute(f"SELECT COUNT(*) FROM {self._table} WHERE athlete_id = ?", (athlete_id,)).fetchone()[0]

    def retention(self, athlete_id: str) -> Retention:
        """How many rows are retained for the athlete and the UTC timestamps of the oldest and newest, in one read.

        Describes the rows present now: equal-instant replacement and the newest-N trim have already been
        applied. Nothing is inferred about rows that were never stored or have been trimmed.
        """
        with self.db.read() as c:
            n, lo, hi = c.execute(
                f"SELECT COUNT(*), MIN(ts_utc), MAX(ts_utc) FROM {self._table} WHERE athlete_id = ?", (athlete_id,)
            ).fetchone()
        return Retention(n, datetime.fromisoformat(lo) if lo else None, datetime.fromisoformat(hi) if hi else None)

    def clear(self) -> None:
        with self.db.transaction() as c:
            c.execute(f"DELETE FROM {self._table}")
