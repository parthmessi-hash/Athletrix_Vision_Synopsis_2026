"""Individual-performance evidence observations of recordings, persisted in SQLite (`recording_performance`)."""

from __future__ import annotations

import secrets
from datetime import datetime

from app.db import Database, get_database, to_utc_iso
from app.models.performance import PerformanceObservation, PerformanceObservationCreate


def new_observation_id(created_at: datetime) -> str:
    return f"obs-{created_at.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"


def insert(c, o: PerformanceObservation) -> None:
    c.execute("INSERT INTO recording_performance (id, recording_id, athlete_id, period_start_utc, created_at, payload) VALUES (?, ?, ?, ?, ?, ?)",
              (o.id, o.recording_id, o.athlete_id, to_utc_iso(o.period_start), to_utc_iso(o.created_at), o.model_dump_json()))


class PerformanceStore:
    def __init__(self, db: Database | None = None) -> None:
        self._db = db

    @property
    def db(self) -> Database:
        return self._db or get_database()

    def add(self, recording_id: str, body: PerformanceObservationCreate, created_at: datetime) -> PerformanceObservation:
        o = PerformanceObservation(**body.model_dump(), id=new_observation_id(created_at), recording_id=recording_id, created_at=created_at)
        with self.db.transaction() as c:
            while c.execute("SELECT 1 FROM recording_performance WHERE id = ?", (o.id,)).fetchone():
                o = o.model_copy(update={"id": new_observation_id(created_at)})
            insert(c, o)
        return o

    def list(self, recording_id: str, athlete_id: str | None = None) -> list[PerformanceObservation]:
        sql, params = "SELECT payload FROM recording_performance WHERE recording_id = ?", [recording_id]
        if athlete_id is not None:
            sql += " AND athlete_id = ?"
            params.append(athlete_id)
        with self.db.read() as c:
            rows = c.execute(sql + " ORDER BY period_start_utc, created_at, id", params).fetchall()
        return [PerformanceObservation.model_validate_json(r["payload"]) for r in rows]

    def delete(self, recording_id: str, athlete_id: str, observation_id: str) -> bool:
        with self.db.transaction() as c:
            return c.execute("DELETE FROM recording_performance WHERE id = ? AND recording_id = ? AND athlete_id = ?", (observation_id, recording_id, athlete_id)).rowcount > 0


performance_store = PerformanceStore()
