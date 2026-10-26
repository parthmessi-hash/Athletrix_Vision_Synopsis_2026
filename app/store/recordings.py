"""Explicit local recordings, persisted in SQLite next to the rolling stores.

A recording names a fixed set of registered athletes and preserves the samples accepted for them while it is active,
in its own tables, outside the rolling retention caps. Only one recording can be active at a time (enforced inside the
write transaction and by a partial unique index). Membership is decided at ingestion: a sample accepted while the
recording is active, for one of its athletes, is written to the recording in the same transaction as the rolling
store, with the same equal-instant replacement within (recording, athlete, stream). Stopping sets `stopped_at`; a
later sample never enters a stopped recording. Nothing here deletes or uploads anything.
"""

from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Sequence
from datetime import datetime

from app.db import Database, get_database, to_utc_iso
from app.models import Athlete, PhysiologicalSample, TrackingSample
from app.models.recording import Recording
from app.store.samples import Retention

TABLES = {"physiological": "recording_physiological_samples", "tracking": "recording_tracking_samples"}
MODELS = {"physiological": PhysiologicalSample, "tracking": TrackingSample}


class RecordingConflict(Exception):
    """Another recording is already active."""

    def __init__(self, active: Recording) -> None:
        super().__init__(f"recording {active.id!r} ({active.name!r}) is already active; stop it first")
        self.active = active


def new_recording_id(started_at: datetime) -> str:
    return f"rec-{started_at.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"


class RecordingStore:
    def __init__(self, db: Database | None = None) -> None:
        self._db = db

    @property
    def db(self) -> Database:
        return self._db or get_database()

    # ---- rows -> models ------------------------------------------------------------------------------------------
    def _athletes(self, c: sqlite3.Connection, recording_id: str) -> list[Athlete]:
        rows = c.execute("SELECT athlete_id AS id, name, jersey_number, sport, team_id FROM recording_athletes WHERE recording_id = ? ORDER BY rowid", (recording_id,)).fetchall()
        return [Athlete(**dict(r)) for r in rows]

    def _recording(self, c: sqlite3.Connection, row) -> Recording:
        athletes = self._athletes(c, row["id"])
        return Recording(
            id=row["id"], name=row["name"], status="stopped" if row["stopped_at"] else "active",
            started_at=datetime.fromisoformat(row["started_at"]), stopped_at=datetime.fromisoformat(row["stopped_at"]) if row["stopped_at"] else None,
            athlete_ids=[a.id for a in athletes], athletes=athletes,
        )

    def _get_in(self, c: sqlite3.Connection, recording_id: str) -> Recording | None:
        row = c.execute("SELECT id, name, started_at, stopped_at FROM recordings WHERE id = ?", (recording_id,)).fetchone()
        return self._recording(c, row) if row else None

    def active_in(self, c: sqlite3.Connection) -> Recording | None:
        row = c.execute("SELECT id, name, started_at, stopped_at FROM recordings WHERE stopped_at IS NULL").fetchone()
        return self._recording(c, row) if row else None

    # ---- lifecycle ---------------------------------------------------------------------------------------------------
    def start(self, name: str, athletes: Sequence[Athlete], started_at: datetime) -> Recording:
        """Create an active recording with a roster snapshot. Raises RecordingConflict if one is already active."""
        with self.db.transaction() as c:
            active = self.active_in(c)
            if active is not None:
                raise RecordingConflict(active)
            recording_id = new_recording_id(started_at)
            c.execute("INSERT INTO recordings (id, name, started_at, stopped_at) VALUES (?, ?, ?, NULL)", (recording_id, name, to_utc_iso(started_at)))
            for a in athletes:
                c.execute("INSERT INTO recording_athletes (recording_id, athlete_id, name, jersey_number, sport, team_id) VALUES (?, ?, ?, ?, ?, ?)",
                          (recording_id, a.id, a.name, a.jersey_number, a.sport, a.team_id))
            return self._get_in(c, recording_id)

    def stop(self, recording_id: str, stopped_at: datetime) -> Recording | None:
        """Stop the recording; an already stopped one is returned unchanged. None if unknown."""
        with self.db.transaction() as c:
            c.execute("UPDATE recordings SET stopped_at = ? WHERE id = ? AND stopped_at IS NULL", (to_utc_iso(stopped_at), recording_id))
            return self._get_in(c, recording_id)

    def get(self, recording_id: str) -> Recording | None:
        with self.db.read() as c:
            return self._get_in(c, recording_id)

    def active(self) -> Recording | None:
        with self.db.read() as c:
            return self.active_in(c)

    def list(self) -> list[Recording]:
        """All recordings, newest start first."""
        with self.db.read() as c:
            rows = c.execute("SELECT id, name, started_at, stopped_at FROM recordings ORDER BY started_at DESC, id DESC").fetchall()
            return [self._recording(c, r) for r in rows]

    # ---- samples ---------------------------------------------------------------------------------------------------------
    def write_sample(self, c: sqlite3.Connection, recording_id: str, stream: str, sample) -> None:
        """Insert or replace (equal instant) one accepted sample into the recording, inside the caller's transaction."""
        c.execute(
            f"INSERT INTO {TABLES[stream]} (recording_id, athlete_id, ts_utc, payload) VALUES (?, ?, ?, ?) "
            f"ON CONFLICT(recording_id, athlete_id, ts_utc) DO UPDATE SET payload = excluded.payload",
            (recording_id, sample.athlete_id, to_utc_iso(sample.timestamp), sample.model_dump_json()),
        )

    def retention(self, recording_id: str, athlete_id: str, stream: str) -> Retention:
        """Count and UTC bounds of the recorded rows for the athlete and stream."""
        with self.db.read() as c:
            n, lo, hi = c.execute(f"SELECT COUNT(*), MIN(ts_utc), MAX(ts_utc) FROM {TABLES[stream]} WHERE recording_id = ? AND athlete_id = ?", (recording_id, athlete_id)).fetchone()
        return Retention(n, datetime.fromisoformat(lo) if lo else None, datetime.fromisoformat(hi) if hi else None)

    def between(self, recording_id: str, athlete_id: str, stream: str, start: datetime, end: datetime) -> list:
        """Recorded samples with start <= timestamp <= end, oldest to newest."""
        with self.db.read() as c:
            rows = c.execute(f"SELECT payload FROM {TABLES[stream]} WHERE recording_id = ? AND athlete_id = ? AND ts_utc BETWEEN ? AND ? ORDER BY ts_utc ASC",
                             (recording_id, athlete_id, to_utc_iso(start), to_utc_iso(end))).fetchall()
        return [MODELS[stream].model_validate_json(r["payload"]) for r in rows]

    def all(self, recording_id: str, athlete_id: str, stream: str) -> list:
        """Every recorded sample for the athlete and stream, oldest to newest."""
        with self.db.read() as c:
            rows = c.execute(f"SELECT payload FROM {TABLES[stream]} WHERE recording_id = ? AND athlete_id = ? ORDER BY ts_utc ASC", (recording_id, athlete_id)).fetchall()
        return [MODELS[stream].model_validate_json(r["payload"]) for r in rows]


recording_store = RecordingStore()
