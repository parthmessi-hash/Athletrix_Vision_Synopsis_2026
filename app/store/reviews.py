"""Saved reviews of recordings, persisted in SQLite (`recording_reviews`); written once, never edited (app/models/review.py)."""

from __future__ import annotations

import secrets
from datetime import datetime

from app.db import Database, get_database, to_utc_iso
from app.models.review import SavedReview


def new_review_id(created_at: datetime) -> str:
    return f"rev-{created_at.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"


def insert(c, r: SavedReview) -> None:
    c.execute("INSERT INTO recording_reviews (id, recording_id, athlete_id, kind, at_utc, created_at, payload) VALUES (?, ?, ?, ?, ?, ?, ?)",
              (r.id, r.recording_id, r.athlete_id, r.kind, to_utc_iso(r.at), to_utc_iso(r.created_at), r.model_dump_json()))


class ReviewStore:
    def __init__(self, db: Database | None = None) -> None:
        self._db = db

    @property
    def db(self) -> Database:
        return self._db or get_database()

    def add(self, r: SavedReview) -> SavedReview:
        with self.db.transaction() as c:
            while c.execute("SELECT 1 FROM recording_reviews WHERE id = ?", (r.id,)).fetchone():
                r = r.model_copy(update={"id": new_review_id(r.created_at)})
            insert(c, r)
        return r

    def list(self, recording_id: str, athlete_id: str | None = None) -> list[SavedReview]:
        sql, params = "SELECT payload FROM recording_reviews WHERE recording_id = ?", [recording_id]
        if athlete_id is not None:
            sql += " AND athlete_id = ?"
            params.append(athlete_id)
        with self.db.read() as c:
            rows = c.execute(sql + " ORDER BY at_utc, created_at, id", params).fetchall()
        return [SavedReview.model_validate_json(r["payload"]) for r in rows]

    def get(self, recording_id: str, review_id: str) -> SavedReview | None:
        with self.db.read() as c:
            row = c.execute("SELECT payload FROM recording_reviews WHERE id = ? AND recording_id = ?", (review_id, recording_id)).fetchone()
        return SavedReview.model_validate_json(row["payload"]) if row else None


review_store = ReviewStore()
