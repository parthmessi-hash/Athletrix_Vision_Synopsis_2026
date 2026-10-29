"""SQLite-backed per-athlete sample store: bounded, timestamp-ordered history whose newest row is the "latest".

One table per stream. Latest-sample reads and history reads come from the same rows, so they can
never disagree. Within a single transaction an insert replaces any row with the same UTC instant
(equal-timestamp => most recent arrival wins) and then trims the oldest rows beyond the stream's cap.
Each stream has its own configured cap (see app/store/history.py), resolved when a sample is written,
so the two streams never need to share one; an explicit `max_samples` pins a store to a fixed cap
(tests and tools). The cap is applied only by writes: opening a database prunes nothing.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import NamedTuple
from typing import Generic, Protocol, TypeVar

from pydantic import BaseModel

from app.db import Database, get_database, to_utc_iso
from app.store.history import retention_cap


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
    def __init__(self, table: str, model: type[S], max_samples: int | None = None, db: Database | None = None) -> None:
        if table not in ("physiological_samples", "tracking_samples"):
            raise ValueError(f"unknown sample table {table!r}")
        if max_samples is not None and (not isinstance(max_samples, int) or isinstance(max_samples, bool) or max_samples < 1):
            raise ValueError(f"max_samples must be a positive integer, got {max_samples!r}")
        self._table = table
        self._model = model
        self._max = max_samples   # None: the stream's configured cap, read at each write
        self._db = db

    @property
    def stream(self) -> str:
        return "physiological" if self._table == "physiological_samples" else "tracking"

    @property
    def max_samples(self) -> int:
        """The cap this store trims to: the explicit one it was built with, else the stream's configured cap."""
        return self._max if self._max is not None else retention_cap(self.stream)

    @property
    def db(self) -> Database:
        return self._db or get_database()

    def _row(self, row) -> S:
        return self._model.model_validate_json(row["payload"])

    # ---- writes -----------------------------------------------------------
    def add(self, sample: S) -> None:
        """Insert (or replace an equal-instant row) and trim to the stream's cap, atomically, in a transaction of its own."""
        with self.db.transaction() as c:
            self.write(c, sample)

    def write(self, c, sample: S, receipt: str | None = None) -> None:
        """The insert-or-replace plus trim, inside the caller's transaction (so other writes can commit with it or not at all).

        `receipt` is the optional receipt metadata (JSON text); an equal-instant replacement replaces it together with
        the payload, so a row's receipt always belongs to the values it holds. The trim keeps the newest `max_samples` rows by recorded timestamp for this athlete and stream only; other
        athletes and the other stream are untouched. This is the only place a cap is applied.
        """
        athlete_id = sample.athlete_id  # type: ignore[attr-defined]
        ts = to_utc_iso(sample.timestamp)  # type: ignore[attr-defined]
        t = self._table
        cap = self.max_samples
        c.execute(
            f"INSERT INTO {t} (athlete_id, ts_utc, payload, receipt) VALUES (?, ?, ?, ?) "
            f"ON CONFLICT(athlete_id, ts_utc) DO UPDATE SET payload = excluded.payload, receipt = excluded.receipt",
            (athlete_id, ts, sample.model_dump_json(), receipt),
        )
        c.execute(
            f"DELETE FROM {t} WHERE athlete_id = ? AND rowid IN "
            f"(SELECT rowid FROM {t} WHERE athlete_id = ? ORDER BY ts_utc DESC LIMIT -1 OFFSET ?)",
            (athlete_id, athlete_id, cap),
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

    def latest_at_or_before(self, athlete_id: str, instant: datetime) -> S | None:
        """Newest sample whose recorded timestamp is at or before `instant` (compared in UTC), or None."""
        with self.db.read() as c:
            row = c.execute(
                f"SELECT payload FROM {self._table} WHERE athlete_id = ? AND ts_utc <= ? ORDER BY ts_utc DESC LIMIT 1",
                (athlete_id, to_utc_iso(instant)),
            ).fetchone()
        return self._row(row) if row else None

    def recent(self, athlete_id: str, limit: int) -> list[S]:
        """The newest `limit` samples, oldest to newest."""
        with self.db.read() as c:
            rows = c.execute(f"SELECT payload FROM {self._table} WHERE athlete_id = ? ORDER BY ts_utc DESC LIMIT ?", (athlete_id, limit)).fetchall()
        return [self._row(r) for r in reversed(rows)]

    def all(self, athlete_id: str) -> list[S]:
        """Every retained sample for the athlete, oldest to newest, whatever the cap is."""
        with self.db.read() as c:
            rows = c.execute(f"SELECT payload FROM {self._table} WHERE athlete_id = ? ORDER BY ts_utc ASC", (athlete_id,)).fetchall()
        return [self._row(r) for r in rows]

    def between(self, athlete_id: str, start: datetime, end: datetime) -> list[S]:
        """Samples with start <= timestamp <= end (compared in UTC), oldest to newest."""
        with self.db.read() as c:
            rows = c.execute(
                f"SELECT payload FROM {self._table} WHERE athlete_id = ? AND ts_utc BETWEEN ? AND ? ORDER BY ts_utc ASC",
                (athlete_id, to_utc_iso(start), to_utc_iso(end)),
            ).fetchall()
        return [self._row(r) for r in rows]

    def receipt_at(self, athlete_id: str, instant: datetime) -> dict | None:
        """Receipt metadata of the row stored at exactly `instant` for the athlete, or None (no row, or not recorded)."""
        with self.db.read() as c:
            row = c.execute(f"SELECT receipt FROM {self._table} WHERE athlete_id = ? AND ts_utc = ?", (athlete_id, to_utc_iso(instant))).fetchone()
        return json.loads(row["receipt"]) if row and row["receipt"] else None

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
