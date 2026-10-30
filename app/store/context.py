"""Coach-context entries of recordings, persisted in SQLite (`recording_context`). Entries are never edited in place:
a correction is a new entry with the same effective_from (the later-created one wins), so the history stays reviewable."""

from __future__ import annotations

import secrets
from datetime import datetime

from app.db import Database, get_database, to_utc_iso
from app.models.context import ContextCreate, ContextEntry


def new_context_id(created_at: datetime) -> str:
    return f"ctx-{created_at.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"


def insert(c, entry: ContextEntry) -> None:
    c.execute("INSERT INTO recording_context (id, recording_id, athlete_id, effective_from_utc, created_at, payload) VALUES (?, ?, ?, ?, ?, ?)",
              (entry.id, entry.recording_id, entry.athlete_id, to_utc_iso(entry.effective_from), to_utc_iso(entry.created_at), entry.model_dump_json()))


def select_effective(entries: list[ContextEntry], at: datetime) -> ContextEntry | None:
    """The entry in force at `at` among entries of one scope (see app.models.context.EFFECTIVE_RULE)."""
    live = [e for e in entries if e.effective_from <= at and (e.effective_to is None or at < e.effective_to)]
    return max(live, key=lambda e: (e.effective_from, e.created_at, e.id), default=None)


class ContextStore:
    def __init__(self, db: Database | None = None) -> None:
        self._db = db

    @property
    def db(self) -> Database:
        return self._db or get_database()

    def add(self, recording_id: str, body: ContextCreate, created_at: datetime) -> ContextEntry:
        entry = ContextEntry(**body.model_dump(), id=new_context_id(created_at), recording_id=recording_id, created_at=created_at)
        with self.db.transaction() as c:
            while c.execute("SELECT 1 FROM recording_context WHERE id = ?", (entry.id,)).fetchone():
                entry = entry.model_copy(update={"id": new_context_id(created_at)})
            insert(c, entry)
        return entry

    def list(self, recording_id: str, athlete_id: str | None = None, *, team_only: bool = False) -> list[ContextEntry]:
        """Entries of the recording by effective_from then creation; optionally one athlete's, or the team's."""
        sql, params = "SELECT payload FROM recording_context WHERE recording_id = ?", [recording_id]
        if team_only:
            sql += " AND athlete_id IS NULL"
        elif athlete_id is not None:
            sql += " AND athlete_id = ?"
            params.append(athlete_id)
        with self.db.read() as c:
            rows = c.execute(sql + " ORDER BY effective_from_utc, created_at, id", params).fetchall()
        return [ContextEntry.model_validate_json(r["payload"]) for r in rows]

    def effective(self, recording_id: str, at: datetime, athlete_id: str | None) -> tuple[ContextEntry | None, ContextEntry | None]:
        team = select_effective(self.list(recording_id, team_only=True), at)
        athlete = select_effective(self.list(recording_id, athlete_id), at) if athlete_id is not None else None
        return team, athlete


context_store = ContextStore()
