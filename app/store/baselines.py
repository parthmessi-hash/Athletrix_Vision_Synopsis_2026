"""Athlete physiological baselines, persisted in SQLite (`athlete_baselines`). The statistics are written once; only the
review status changes, and every decision is appended to the stored history (app/models/baseline.py)."""

from __future__ import annotations

import secrets
from datetime import datetime

from app.db import Database, get_database, to_utc_iso
from app.models.baseline import Baseline, RecordedDecision


def new_baseline_id(created_at: datetime) -> str:
    return f"bl-{created_at.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"


def insert(c, b: Baseline) -> None:
    c.execute("INSERT INTO athlete_baselines (id, athlete_id, source_recording_id, version, created_at, payload) VALUES (?, ?, ?, ?, ?, ?)",
              (b.id, b.athlete_id, b.source_recording_id, b.version, to_utc_iso(b.created_at), b.model_dump_json()))


def next_version(c, athlete_id: str) -> int:
    return (c.execute("SELECT MAX(version) FROM athlete_baselines WHERE athlete_id = ?", (athlete_id,)).fetchone()[0] or 0) + 1


class BaselineStore:
    def __init__(self, db: Database | None = None) -> None:
        self._db = db

    @property
    def db(self) -> Database:
        return self._db or get_database()

    def add(self, b: Baseline) -> Baseline:
        with self.db.transaction() as c:
            b = b.model_copy(update={"version": next_version(c, b.athlete_id)})
            while c.execute("SELECT 1 FROM athlete_baselines WHERE id = ?", (b.id,)).fetchone():
                b = b.model_copy(update={"id": new_baseline_id(b.created_at)})
            insert(c, b)
        return b

    def list(self, athlete_id: str) -> list[Baseline]:
        with self.db.read() as c:
            rows = c.execute("SELECT payload FROM athlete_baselines WHERE athlete_id = ? ORDER BY version", (athlete_id,)).fetchall()
        return [Baseline.model_validate_json(r["payload"]) for r in rows]

    def for_recording(self, recording_id: str) -> list[Baseline]:
        with self.db.read() as c:
            rows = c.execute("SELECT payload FROM athlete_baselines WHERE source_recording_id = ? ORDER BY athlete_id, version", (recording_id,)).fetchall()
        return [Baseline.model_validate_json(r["payload"]) for r in rows]

    def get(self, athlete_id: str, baseline_id: str) -> Baseline | None:
        with self.db.read() as c:
            row = c.execute("SELECT payload FROM athlete_baselines WHERE id = ? AND athlete_id = ?", (baseline_id, athlete_id)).fetchone()
        return Baseline.model_validate_json(row["payload"]) if row else None

    def decide(self, athlete_id: str, baseline_id: str, decision: RecordedDecision) -> Baseline | None:
        with self.db.transaction() as c:
            row = c.execute("SELECT payload FROM athlete_baselines WHERE id = ? AND athlete_id = ?", (baseline_id, athlete_id)).fetchone()
            if row is None:
                return None
            b = Baseline.model_validate_json(row["payload"])
            b = b.model_copy(update={"status": decision.decision, "decisions": [*b.decisions, decision]})
            c.execute("UPDATE athlete_baselines SET payload = ? WHERE id = ?", (b.model_dump_json(), b.id))
        return b


baseline_store = BaselineStore()
