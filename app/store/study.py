"""Study records (`recording_study`): append-only, idempotent per client token (app/models/study.py)."""

from __future__ import annotations

import secrets
from datetime import datetime

from app.db import Database, get_database, to_utc_iso
from app.models.study import StudyEntry


def new_study_id(created_at: datetime, kind: str) -> str:
    return f"st-{kind[:4]}-{created_at.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"


def insert(c, e: StudyEntry) -> None:
    c.execute("INSERT INTO recording_study (id, recording_id, athlete_id, kind, client_token, at_utc, created_at, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
              (e.id, e.recording_id, e.athlete_id, e.kind, e.client_token, to_utc_iso(e.at), to_utc_iso(e.created_at), e.model_dump_json()))


class StudyStore:
    def __init__(self, db: Database | None = None) -> None:
        self._db = db

    @property
    def db(self) -> Database:
        return self._db or get_database()

    def by_token(self, c, recording_id: str, token: str) -> StudyEntry | None:
        row = c.execute("SELECT payload FROM recording_study WHERE recording_id = ? AND client_token = ?", (recording_id, token)).fetchone()
        return StudyEntry.model_validate_json(row["payload"]) if row else None

    def list(self, recording_id: str) -> list[StudyEntry]:
        with self.db.read() as c:
            rows = c.execute("SELECT payload FROM recording_study WHERE recording_id = ? ORDER BY at_utc, created_at, rowid", (recording_id,)).fetchall()
        return [StudyEntry.model_validate_json(r["payload"]) for r in rows]


study_store = StudyStore()


def current(entries: list[StudyEntry], known_at: datetime | None = None) -> list[StudyEntry]:
    """Entries in force: not superseded (by an entry known by `known_at`, when given)."""
    known = [e for e in entries if known_at is None or e.created_at <= known_at]
    gone = {e.supersedes for e in known if e.supersedes}
    return [e for e in known if e.id not in gone]
