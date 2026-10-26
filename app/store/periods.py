"""Saved effort periods of stopped recordings, persisted in SQLite. A selection only: label and exact instants."""

from __future__ import annotations

import secrets
from datetime import datetime

from app.db import Database, get_database, to_utc_iso
from app.models.period import SavedPeriod


def new_period_id(created_at: datetime) -> str:
    return f"per-{created_at.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"


class PeriodStore:
    def __init__(self, db: Database | None = None) -> None:
        self._db = db

    @property
    def db(self) -> Database:
        return self._db or get_database()

    @staticmethod
    def _row(row) -> SavedPeriod:
        start, end = datetime.fromisoformat(row["effort_start"]), datetime.fromisoformat(row["effort_end"])
        return SavedPeriod(id=row["id"], recording_id=row["recording_id"], athlete_id=row["athlete_id"], label=row["label"], effort_start=start, effort_end=end,
                           duration_seconds=(end - start).total_seconds(), created_at=datetime.fromisoformat(row["created_at"]))

    def add(self, recording_id: str, athlete_id: str, label: str, effort_start: datetime, effort_end: datetime, created_at: datetime) -> SavedPeriod:
        """Persist the selection with its instants exactly as given (offsets kept) plus UTC copies for ordering."""
        period_id = new_period_id(created_at)
        with self.db.transaction() as c:
            c.execute("INSERT INTO recording_periods (id, recording_id, athlete_id, label, effort_start, effort_end, start_utc, end_utc, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (period_id, recording_id, athlete_id, label, effort_start.isoformat(), effort_end.isoformat(), to_utc_iso(effort_start), to_utc_iso(effort_end), to_utc_iso(created_at)))
            row = c.execute("SELECT * FROM recording_periods WHERE id = ?", (period_id,)).fetchone()
        return self._row(row)

    def get(self, recording_id: str, athlete_id: str, period_id: str) -> SavedPeriod | None:
        with self.db.read() as c:
            row = c.execute("SELECT * FROM recording_periods WHERE id = ? AND recording_id = ? AND athlete_id = ?", (period_id, recording_id, athlete_id)).fetchone()
        return self._row(row) if row else None

    def list(self, recording_id: str, athlete_id: str) -> list[SavedPeriod]:
        """The athlete's saved periods in this recording, by start instant then creation time."""
        with self.db.read() as c:
            rows = c.execute("SELECT * FROM recording_periods WHERE recording_id = ? AND athlete_id = ? ORDER BY start_utc, created_at, id", (recording_id, athlete_id)).fetchall()
        return [self._row(r) for r in rows]


period_store = PeriodStore()
