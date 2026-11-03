"""Roster-state and match-constraint entries of recordings, persisted in SQLite (`recording_roster`); never edited in place."""

from __future__ import annotations

import secrets
from datetime import datetime

from app.db import Database, get_database, to_utc_iso
from app.models.roster import RosterCreate, RosterEntry
from app.store.context import select_effective


def new_roster_id(created_at: datetime) -> str:
    return f"ros-{created_at.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"


def insert(c, e: RosterEntry) -> None:
    c.execute("INSERT INTO recording_roster (id, recording_id, athlete_id, effective_from_utc, created_at, payload) VALUES (?, ?, ?, ?, ?, ?)",
              (e.id, e.recording_id, e.athlete_id, to_utc_iso(e.effective_from), to_utc_iso(e.created_at), e.model_dump_json()))


def effective_roster(entries: list[RosterEntry], at: datetime) -> tuple[RosterEntry | None, dict[str, RosterEntry]]:
    """(team entry in force, athlete id -> athlete entry in force) at `at`, by the coach-context rule."""
    team = select_effective([e for e in entries if e.athlete_id is None], at)
    athletes = {}
    for a in {e.athlete_id for e in entries if e.athlete_id}:
        e = select_effective([x for x in entries if x.athlete_id == a], at)
        if e is not None:
            athletes[a] = e
    return team, athletes


class RosterStore:
    def __init__(self, db: Database | None = None) -> None:
        self._db = db

    @property
    def db(self) -> Database:
        return self._db or get_database()

    def add(self, recording_id: str, body: RosterCreate, created_at: datetime) -> RosterEntry:
        e = RosterEntry(**body.model_dump(), id=new_roster_id(created_at), recording_id=recording_id, created_at=created_at)
        with self.db.transaction() as c:
            while c.execute("SELECT 1 FROM recording_roster WHERE id = ?", (e.id,)).fetchone():
                e = e.model_copy(update={"id": new_roster_id(created_at)})
            insert(c, e)
        return e

    def list(self, recording_id: str) -> list[RosterEntry]:
        with self.db.read() as c:
            rows = c.execute("SELECT payload FROM recording_roster WHERE recording_id = ? ORDER BY effective_from_utc, created_at, id", (recording_id,)).fetchall()
        return [RosterEntry.model_validate_json(r["payload"]) for r in rows]


roster_store = RosterStore()
