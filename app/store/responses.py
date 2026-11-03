"""Coach responses of recordings, persisted in SQLite (`recording_responses`); written once (app/models/response.py)."""

from __future__ import annotations

import secrets
from datetime import datetime

from app.db import Database, get_database, to_utc_iso
from app.models.response import CoachResponse


def new_response_id(created_at: datetime) -> str:
    return f"resp-{created_at.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"


def insert(c, r: CoachResponse) -> None:
    c.execute("INSERT INTO recording_responses (id, recording_id, athlete_id, client_token, created_at, payload) VALUES (?, ?, ?, ?, ?, ?)",
              (r.id, r.recording_id, r.athlete_id, r.client_token, to_utc_iso(r.created_at), r.model_dump_json()))


class ResponseStore:
    def __init__(self, db: Database | None = None) -> None:
        self._db = db

    @property
    def db(self) -> Database:
        return self._db or get_database()

    def by_token(self, c, recording_id: str, token: str) -> CoachResponse | None:
        row = c.execute("SELECT payload FROM recording_responses WHERE recording_id = ? AND client_token = ?", (recording_id, token)).fetchone()
        return CoachResponse.model_validate_json(row["payload"]) if row else None

    def list(self, recording_id: str, athlete_id: str | None = None) -> list[CoachResponse]:
        sql, params = "SELECT payload FROM recording_responses WHERE recording_id = ?", [recording_id]
        if athlete_id is not None:
            sql += " AND athlete_id = ?"
            params.append(athlete_id)
        with self.db.read() as c:
            rows = c.execute(sql + " ORDER BY created_at, id", params).fetchall()
        return [CoachResponse.model_validate_json(r["payload"]) for r in rows]


response_store = ResponseStore()
