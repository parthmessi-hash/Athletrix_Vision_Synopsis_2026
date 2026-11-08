"""Rejected sample writes (`ingest_rejections`): when, which stream, which athlete (when the body named one) and why.

A rejected write stores no sample, so without this log a rejected sample and a sample that never arrived look the same.
Bounded: only the newest MAX_ROWS rows are kept. Nothing here is a sample or evidence; it is operational metadata.
"""

from __future__ import annotations

from datetime import datetime

from app.db import Database, get_database, to_utc_iso

MAX_ROWS = 5000


class RejectionStore:
    def __init__(self, db: Database | None = None) -> None:
        self._db = db

    @property
    def db(self) -> Database:
        return self._db or get_database()

    def add(self, received_at: datetime, stream: str, athlete_id: str | None, reason: str) -> None:
        with self.db.transaction() as c:
            c.execute("INSERT INTO ingest_rejections (received_at, stream, athlete_id, reason) VALUES (?, ?, ?, ?)",
                      (to_utc_iso(received_at), stream, athlete_id, reason[:200]))
            c.execute("DELETE FROM ingest_rejections WHERE id <= (SELECT MAX(id) FROM ingest_rejections) - ?", (MAX_ROWS,))

    def counts(self, start: datetime, end: datetime) -> dict[tuple[str, str], dict]:
        """(athlete_id, stream) -> {"count", "last_at", "last_reason"} for rejections received in (start, end]."""
        with self.db.read() as c:
            rows = c.execute("SELECT athlete_id, stream, COUNT(*) AS n, MAX(received_at) AS last_at, "
                             "(SELECT reason FROM ingest_rejections j WHERE j.athlete_id = r.athlete_id AND j.stream = r.stream ORDER BY j.id DESC LIMIT 1) AS reason "
                             "FROM ingest_rejections r WHERE received_at > ? AND received_at <= ? AND athlete_id IS NOT NULL GROUP BY athlete_id, stream",
                             (to_utc_iso(start), to_utc_iso(end))).fetchall()
        return {(r["athlete_id"], r["stream"]): {"count": r["n"], "last_at": r["last_at"], "last_reason": r["reason"]} for r in rows}


rejection_store = RejectionStore()
