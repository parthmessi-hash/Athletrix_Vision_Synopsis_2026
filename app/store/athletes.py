"""Athlete registry keyed by Athlete.id, persisted in SQLite.

Registration is not required for sample ingestion; unregistered athlete_ids are still accepted by
the sample endpoints.
"""

from __future__ import annotations

import sqlite3

from app.db import Database, get_database
from app.models import Athlete


class AthleteRegistry:
    def __init__(self, db: Database | None = None) -> None:
        self._db = db

    @property
    def db(self) -> Database:
        return self._db or get_database()

    def add(self, athlete: Athlete) -> bool:
        """Register the athlete. Returns False, without overwriting, if the id already exists."""
        with self.db.transaction() as c:
            try:
                c.execute(
                    "INSERT INTO athletes (id, name, jersey_number, sport, team_id) VALUES (?, ?, ?, ?, ?)",
                    (athlete.id, athlete.name, athlete.jersey_number, athlete.sport, athlete.team_id),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    def get(self, athlete_id: str) -> Athlete | None:
        with self.db.read() as c:
            row = c.execute("SELECT id, name, jersey_number, sport, team_id FROM athletes WHERE id = ?", (athlete_id,)).fetchone()
        return Athlete(**dict(row)) if row else None

    def list(self) -> list[Athlete]:
        """All registered athletes sorted by id."""
        with self.db.read() as c:
            rows = c.execute("SELECT id, name, jersey_number, sport, team_id FROM athletes ORDER BY id").fetchall()
        return [Athlete(**dict(r)) for r in rows]

    def clear(self) -> None:
        with self.db.transaction() as c:
            c.execute("DELETE FROM athletes")


athlete_registry = AthleteRegistry()
