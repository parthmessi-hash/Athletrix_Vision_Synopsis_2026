"""Import a validated portable recording package into a local database, atomically, with provenance.

Everything is written in one BEGIN IMMEDIATE transaction: the recording (stopped, original start and stop times, a new
id), its roster snapshot, the start-time configuration snapshot when the package carries one (origin "source_engine"), every sample exactly as normalised in the package, receipt metadata carried in the package
(stored with origin "source_engine": received by the engine where the recording was made, never by this one; samples
imported from a package without it have none), every saved period (new ids, exact bounds,
original labels and creation times), every coach-context entry (new id, origin "source_engine", original id kept as
source_entry_id, content unchanged), every performance observation (likewise, with its coach_context_id re-pointed to
the imported context entry) and a `recording_imports` row mapping the new ids to the original ones. Any failure
rolls the whole import back. The athlete registry, rolling history, device status and any active recording are never
touched, and nothing existing is overwritten or merged.

Repeated import: a package whose SHA-256 is already recorded in `recording_imports` is refused unless
`allow_duplicate` is set, in which case it becomes another, separate recording with its own new ids and its own
provenance row. It is never merged into the earlier copy.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from app.core.recording_package import Package, iso
from app.db import Database, to_utc_iso
from app.store import context as context_store
from app.store import performance as performance_store
from app.store.periods import new_period_id
from app.store.recordings import TABLES, RecordingStore, new_recording_id


class AlreadyImported(Exception):
    def __init__(self, recording_ids: list[str]) -> None:
        super().__init__(f"this package (same SHA-256) was already imported as {', '.join(recording_ids)}; pass allow_duplicate / --allow-duplicate to import a separate copy")
        self.recording_ids = recording_ids


@dataclass
class ImportResult:
    recording_id: str
    source_recording_id: str
    period_id_map: dict[str, str]
    samples: dict[str, int]
    imported_at: datetime


def previous_imports(c, package_sha256: str) -> list[str]:
    try:
        return [r[0] for r in c.execute("SELECT recording_id FROM recording_imports WHERE package_sha256 = ? ORDER BY imported_at", (package_sha256,)).fetchall()]
    except Exception:  # noqa: BLE001  the table does not exist yet in an older database: nothing was imported
        return []


def import_package(db: Database, pkg: Package, now: datetime, *, allow_duplicate: bool = False, checkpoint: Callable[[str], None] | None = None) -> ImportResult:
    """Write the package as a new stopped recording in one transaction. `checkpoint(stage)` is called between stages (tests use it to force a failure)."""
    rec = pkg.recording
    with db.transaction() as c:
        earlier = previous_imports(c, pkg.package_sha256)
        if earlier and not allow_duplicate:
            raise AlreadyImported(earlier)
        new_id = new_recording_id(now)
        while c.execute("SELECT 1 FROM recordings WHERE id = ?", (new_id,)).fetchone():
            new_id = new_recording_id(now)
        started = datetime.fromisoformat(rec["started_at"].replace("Z", "+00:00"))
        stopped = datetime.fromisoformat(rec["stopped_at"].replace("Z", "+00:00"))
        c.execute("INSERT INTO recordings (id, name, started_at, stopped_at) VALUES (?, ?, ?, ?)", (new_id, rec["name"].strip(), to_utc_iso(started), to_utc_iso(stopped)))
        for a in pkg.athletes:
            c.execute("INSERT INTO recording_athletes (recording_id, athlete_id, name, jersey_number, sport, team_id) VALUES (?, ?, ?, ?, ?, ?)", (new_id, a.id, a.name, a.jersey_number, a.sport, a.team_id))
        if pkg.configuration is not None:   # the source engine's start-time snapshot, kept as such; absent = unavailable
            RecordingStore.write_configuration(c, new_id, "source_engine", pkg.configuration)
        if checkpoint:
            checkpoint("recording")
        counts = {"physiological": 0, "tracking": 0}
        for athlete_id, streams in pkg.samples.items():
            for stream, models in streams.items():
                for m in models:
                    received = pkg.receipts.get((athlete_id, stream, to_utc_iso(m.timestamp)))   # the source engine's receipt, never this engine's
                    receipt = json.dumps({"received_at": received, "origin": "source_engine"}) if received else None
                    c.execute(f"INSERT INTO {TABLES[stream]} (recording_id, athlete_id, ts_utc, payload, receipt) VALUES (?, ?, ?, ?, ?)", (new_id, athlete_id, to_utc_iso(m.timestamp), m.model_dump_json(), receipt))
                    counts[stream] += 1
                if checkpoint:
                    checkpoint(f"samples:{athlete_id}:{stream}")
        id_map: dict[str, str] = {}
        for p in pkg.periods:
            pid = new_period_id(now)
            while pid in id_map.values() or c.execute("SELECT 1 FROM recording_periods WHERE id = ?", (pid,)).fetchone():
                pid = new_period_id(now)
            c.execute("INSERT INTO recording_periods (id, recording_id, athlete_id, label, effort_start, effort_end, start_utc, end_utc, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (pid, new_id, p["athlete_id"], p["label"], p["effort_start"].isoformat(), p["effort_end"].isoformat(), to_utc_iso(p["effort_start"]), to_utc_iso(p["effort_end"]), to_utc_iso(p["created_at"])))
            id_map[p["original_id"]] = pid
        if checkpoint:
            checkpoint("periods")
        context_map: dict[str, str] = {}
        for e in pkg.context:
            cid = context_store.new_context_id(now)
            while c.execute("SELECT 1 FROM recording_context WHERE id = ?", (cid,)).fetchone():
                cid = context_store.new_context_id(now)
            context_map[e.id] = cid
            context_store.insert(c, e.model_copy(update={"id": cid, "recording_id": new_id, "origin": "source_engine", "source_entry_id": e.source_entry_id or e.id}))
        if checkpoint:
            checkpoint("context")
        for o in pkg.performance:
            oid = performance_store.new_observation_id(now)
            while c.execute("SELECT 1 FROM recording_performance WHERE id = ?", (oid,)).fetchone():
                oid = performance_store.new_observation_id(now)
            performance_store.insert(c, o.model_copy(update={"id": oid, "recording_id": new_id, "origin": "source_engine", "source_entry_id": o.source_entry_id or o.id,
                                                             "coach_context_id": context_map.get(o.coach_context_id) if o.coach_context_id else None}))
        if checkpoint:
            checkpoint("performance")
        c.execute("INSERT INTO recording_imports (recording_id, source_recording_id, package_sha256, package_format, package_generated_at, imported_at, period_id_map) VALUES (?, ?, ?, ?, ?, ?, ?)",
                  (new_id, rec["id"], pkg.package_sha256, f"{pkg.manifest['format']}/{pkg.manifest['format_version']}", pkg.manifest["generated_at"], to_utc_iso(now), json.dumps(id_map, sort_keys=True)))
        if checkpoint:
            checkpoint("provenance")
    return ImportResult(recording_id=new_id, source_recording_id=rec["id"], period_id_map=id_map, samples=counts, imported_at=now)


def provenance(c, recording_id: str) -> dict | None:
    """The import provenance of a recording, or None for one recorded on this engine."""
    try:
        row = c.execute("SELECT * FROM recording_imports WHERE recording_id = ?", (recording_id,)).fetchone()
    except Exception:  # noqa: BLE001
        return None
    if row is None:
        return None
    return {"source_recording_id": row["source_recording_id"], "package_sha256": row["package_sha256"], "package_format": row["package_format"],
            "package_generated_at": row["package_generated_at"], "imported_at": iso(datetime.fromisoformat(row["imported_at"])), "period_id_map": json.loads(row["period_id_map"])}
