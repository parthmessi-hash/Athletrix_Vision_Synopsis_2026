"""Inventory and scoped logical removal of stored research data in one local Athletrix database (see scripts/local_data.py).

Two scopes:
  recording  one recording: its row, roster snapshot (recording_athletes), recorded samples, saved periods, start-time
             configuration snapshot and import provenance. Rolling history, the registry, device status and every
             other recording are untouched.
  athlete    one athlete id across the database: registry row, rolling samples, and in every recording the membership
             row, recorded samples and saved periods. A recording shared with other athletes stays valid for them (its
             row, snapshot and the others' data are kept; import provenance loses only the removed period ids). A
             recording whose roster would become empty is removed entirely, because it would hold nothing.
             Device-status rows are keyed by device id, not by athlete, and a device can be reused by another athlete,
             so they are only reported; one is removed only when explicitly named and no other athlete's stored
             samples name that device.

A plan is computed first and reported exactly (tables, row counts, relationships, refusals, limitations). Execution
recomputes the plan inside one exclusive transaction and deletes in it; any failure rolls everything back.

Concurrent access: execution opens the file with SQLite's EXCLUSIVE locking mode and BEGIN EXCLUSIVE. SQLite refuses
that while any other connection that has used the file is attached (a running engine always is, since it creates its
schema on open), and while it is held no engine can open the file. This is SQLite's own file locking, not a process
check; it is unreliable on network file systems that do not implement locks.

Removal is logical deletion. `secure_delete` is enabled for the transaction so SQLite overwrites the freed content it
writes, and the WAL is checkpointed and truncated afterwards, but copies may remain in file-system blocks, snapshots,
backups, exports and packages already made, and anything outside this database file. Not secure erasure.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from app.store.recordings import TABLES as RECORDING_SAMPLE_TABLES

ROLLING_SAMPLE_TABLES = ("physiological_samples", "tracking_samples")
RECORDING_SCOPED = ("recording_athletes", *RECORDING_SAMPLE_TABLES.values(), "recording_periods", "recording_configurations", "recording_imports")

CANNOT_REMOVE = [
    "files exported earlier (JSON Lines, CSV, review JSON) and portable packages already copied anywhere, including other engines' databases that imported them",
    "camera footage, adapter or simulator logs, terminal output and notes kept outside this database",
    "backups, Time Machine or cloud snapshots, copies of this database file and external systems",
    "every physical trace on the storage medium: SQLite deletion is logical, and freed blocks may survive in the file system or SSD",
]


class DatabaseInUse(Exception):
    """Another connection holds the database open (for example a running engine); nothing was changed."""


@dataclass
class Plan:
    scope: str
    target: str
    found: bool = False
    deletes: list[dict] = field(default_factory=list)       # {"table", "count", "detail", "where", "params"}
    updates: list[dict] = field(default_factory=list)       # {"table", "recording_id", "detail", "period_id_map"}
    kept: list[str] = field(default_factory=list)           # related records deliberately left in place, and why
    refusals: list[str] = field(default_factory=list)       # any refusal blocks execution
    limitations: list[str] = field(default_factory=list)

    @property
    def total_rows(self) -> int:
        return sum(d["count"] for d in self.deletes)

    def report(self) -> dict:
        return {"scope": self.scope, "target": self.target, "found": self.found, "executable": self.found and not self.refusals,
                "deletes": [{k: d[k] for k in ("table", "count", "detail")} for d in self.deletes],
                "updates": [{k: u[k] for k in ("table", "recording_id", "detail")} for u in self.updates],
                "rows_to_delete": self.total_rows, "kept": self.kept, "refusals": self.refusals, "limitations": self.limitations,
                "cannot_remove": CANNOT_REMOVE}


# ---- connections ------------------------------------------------------------------------------------------------------------

def open_read_only(path: Path) -> sqlite3.Connection:
    """Read-only connection for inventories and dry runs; never creates the file or its schema."""
    if not path.is_file():
        raise FileNotFoundError(f"{path} does not exist; nothing to inspect (no file was created)")
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def open_exclusive(path: Path) -> sqlite3.Connection:
    """Connection that holds the database exclusively, or DatabaseInUse if another connection has it open."""
    if not path.is_file():
        raise FileNotFoundError(f"{path} does not exist; nothing to remove (no file was created)")
    c = sqlite3.connect(str(path), isolation_level=None, timeout=0.5)
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA locking_mode=EXCLUSIVE")
        c.execute("BEGIN EXCLUSIVE")
    except sqlite3.OperationalError as exc:
        c.close()
        raise DatabaseInUse(f"SQLite could not lock {path} exclusively ({exc}): another connection has it open, such as a running engine. "
                            "Stop the engine that uses this file and try again; nothing was changed.") from exc
    return c


def tables(c: sqlite3.Connection) -> set[str]:
    return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def count(c, table: str, where: str, params: tuple) -> int:
    return c.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()[0]


# ---- inventory --------------------------------------------------------------------------------------------------------------

def inventory(c: sqlite3.Connection) -> dict:
    """Every table's row count, recordings with rosters and sizes, and athlete ids with where their data is."""
    have = tables(c)
    counts = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in sorted(have)}
    recordings = []
    if "recordings" in have:
        for r in c.execute("SELECT id, name, started_at, stopped_at FROM recordings ORDER BY started_at, id").fetchall():
            roster = [a[0] for a in c.execute("SELECT athlete_id FROM recording_athletes WHERE recording_id = ? ORDER BY rowid", (r["id"],))] if "recording_athletes" in have else []
            recordings.append({"id": r["id"], "name": r["name"], "status": "stopped" if r["stopped_at"] else "active", "started_at": r["started_at"], "stopped_at": r["stopped_at"], "roster": roster,
                               **{t: count(c, t, "recording_id = ?", (r["id"],)) for t in RECORDING_SCOPED if t in have and t != "recording_athletes"}})
    ids: set[str] = {r[0] for r in c.execute("SELECT id FROM athletes")} if "athletes" in have else set()
    for t in (*ROLLING_SAMPLE_TABLES, "recording_athletes", *RECORDING_SAMPLE_TABLES.values(), "recording_periods"):
        if t in have:
            ids |= {r[0] for r in c.execute(f"SELECT DISTINCT athlete_id FROM {t}")}
    athletes = []
    for a in sorted(ids):
        athletes.append({"id": a, "registered": "athletes" in have and count(c, "athletes", "id = ?", (a,)) > 0,
                         **{t: count(c, t, "athlete_id = ?", (a,)) for t in (*ROLLING_SAMPLE_TABLES, "recording_athletes", *RECORDING_SAMPLE_TABLES.values(), "recording_periods") if t in have}})
    return {"tables": counts, "recordings": recordings, "athletes": athletes,
            "note": "Counts of rows in this database file only. Device status is keyed by device id, not athlete."}


# ---- plans ------------------------------------------------------------------------------------------------------------------

def _delete(plan: Plan, c, have: set[str], table: str, where: str, params: tuple, detail: str) -> None:
    if table not in have:
        return
    n = count(c, table, where, params)
    if n:
        plan.deletes.append({"table": table, "count": n, "detail": detail, "where": where, "params": params})


def _whole_recording(plan: Plan, c, have: set[str], recording_id: str, why: str) -> None:
    _delete(plan, c, have, "recordings", "id = ?", (recording_id,), f"recording {recording_id} ({why})")
    for t in RECORDING_SCOPED:
        _delete(plan, c, have, t, "recording_id = ?", (recording_id,), f"rows of recording {recording_id}")


def plan_recording(c: sqlite3.Connection, recording_id: str) -> Plan:
    plan = Plan("recording", recording_id)
    have = tables(c)
    row = c.execute("SELECT id, stopped_at FROM recordings WHERE id = ?", (recording_id,)).fetchone() if "recordings" in have else None
    if row is None:
        return plan
    plan.found = True
    if row["stopped_at"] is None:
        plan.refusals.append(f"recording {recording_id} is active; stop it first (an active recording would keep receiving samples)")
    _whole_recording(plan, c, have, recording_id, "the target")
    roster = [r[0] for r in c.execute("SELECT athlete_id FROM recording_athletes WHERE recording_id = ?", (recording_id,))] if "recording_athletes" in have else []
    for a in roster:
        rolling = sum(count(c, t, "athlete_id = ?", (a,)) for t in ROLLING_SAMPLE_TABLES if t in have)
        plan.kept.append(f"athlete {a}: registry entry, {rolling} rolling samples and any other recordings are kept (this scope removes only the recording)")
    if "recording_imports" in have and c.execute("SELECT 1 FROM recording_imports WHERE recording_id = ?", (recording_id,)).fetchone():
        plan.limitations.append("this recording was imported from a portable package; that package file and the engine it came from still hold the data")
    plan.limitations.append("packages or files exported from this recording earlier are not affected")
    return plan


def _device_ids(c, have: set[str], where: str, params: tuple) -> set[str]:
    out: set[str] = set()
    for t in (*ROLLING_SAMPLE_TABLES, *RECORDING_SAMPLE_TABLES.values()):
        if t in have:
            out |= {r[0] for r in c.execute(f"SELECT DISTINCT json_extract(payload, '$.device_id') FROM {t} WHERE {where}", params) if r[0]}
    return out


def plan_athlete(c: sqlite3.Connection, athlete_id: str, device_status_ids: tuple[str, ...] = ()) -> Plan:
    plan = Plan("athlete", athlete_id)
    have = tables(c)
    tables_with_athlete = [t for t in ("athletes", *ROLLING_SAMPLE_TABLES, "recording_athletes", *RECORDING_SAMPLE_TABLES.values(), "recording_periods") if t in have]
    present = any(count(c, t, "id = ?" if t == "athletes" else "athlete_id = ?", (athlete_id,)) for t in tables_with_athlete)
    if not present:
        if device_status_ids:
            plan.refusals.append("device status can only be removed together with an athlete whose stored samples name the device")
        return plan
    plan.found = True
    _delete(plan, c, have, "athletes", "id = ?", (athlete_id,), "registry entry")
    for t in ROLLING_SAMPLE_TABLES:
        _delete(plan, c, have, t, "athlete_id = ?", (athlete_id,), "rolling samples")

    rec_ids: set[str] = set()
    for t in ("recording_athletes", *RECORDING_SAMPLE_TABLES.values(), "recording_periods"):
        if t in have:
            rec_ids |= {r[0] for r in c.execute(f"SELECT DISTINCT recording_id FROM {t} WHERE athlete_id = ?", (athlete_id,))}
    for rec_id in sorted(rec_ids):
        rec = c.execute("SELECT stopped_at FROM recordings WHERE id = ?", (rec_id,)).fetchone() if "recordings" in have else None
        if rec is not None and rec["stopped_at"] is None:
            plan.refusals.append(f"athlete {athlete_id} is in active recording {rec_id}; stop it first")
        roster = [r[0] for r in c.execute("SELECT athlete_id FROM recording_athletes WHERE recording_id = ?", (rec_id,))] if "recording_athletes" in have else []
        others = [a for a in roster if a != athlete_id]
        if not others:
            _whole_recording(plan, c, have, rec_id, f"its only roster member was {athlete_id}")
            continue
        for t in ("recording_athletes", *RECORDING_SAMPLE_TABLES.values(), "recording_periods"):
            _delete(plan, c, have, t, "recording_id = ? AND athlete_id = ?", (rec_id, athlete_id), f"in shared recording {rec_id}")
        plan.kept.append(f"recording {rec_id} stays valid for {', '.join(others)}: its row, configuration snapshot and their samples and periods are kept")
        if "recording_imports" in have:
            imp = c.execute("SELECT period_id_map FROM recording_imports WHERE recording_id = ?", (rec_id,)).fetchone()
            if imp is not None:
                removed = {r[0] for r in c.execute("SELECT id FROM recording_periods WHERE recording_id = ? AND athlete_id = ?", (rec_id, athlete_id))} if "recording_periods" in have else set()
                mapping = json.loads(imp["period_id_map"])
                kept_map = {k: v for k, v in mapping.items() if v not in removed}
                if kept_map != mapping:
                    plan.updates.append({"table": "recording_imports", "recording_id": rec_id, "period_id_map": kept_map,
                                         "detail": f"drop {len(mapping) - len(kept_map)} removed period ids from the import mapping of {rec_id}"})
                plan.limitations.append(f"recording {rec_id} was imported from a portable package; that package and its source engine still hold {athlete_id}'s data")
        if "recording_configurations" in have:
            cfg = c.execute("SELECT snapshot FROM recording_configurations WHERE recording_id = ?", (rec_id,)).fetchone()
            if cfg is not None:
                supplied = json.loads(cfg["snapshot"]).get("operator_supplied") or {}
                if athlete_id in json.dumps(supplied):
                    plan.limitations.append(f"the operator-supplied adapter note of recording {rec_id} mentions {athlete_id!r}; it is free text and was not edited")

    own_devices = _device_ids(c, have, "athlete_id = ?", (athlete_id,))
    other_devices = _device_ids(c, have, "athlete_id != ?", (athlete_id,))
    for d in sorted(own_devices):
        status = "device_status" in have and count(c, "device_status", "device_id = ?", (d,)) > 0
        shared = d in other_devices
        if d in device_status_ids:
            continue
        if not status:
            plan.kept.append(f"device {d}: named by this athlete's samples; no status report is stored")
        elif shared:
            plan.kept.append(f"device {d}: its latest status report is kept; other athletes' samples also name this device")
        else:
            plan.kept.append(f"device {d}: its latest status report is kept (device status is keyed by device id, not athlete); name it with --include-device-status to remove it")
    for d in device_status_ids:
        if d not in own_devices:
            plan.refusals.append(f"device {d}: not named by any stored sample of {athlete_id}; its status is not removed with this athlete")
        elif d in other_devices:
            plan.refusals.append(f"device {d}: other athletes' stored samples also name it; its status cannot be attributed to {athlete_id} alone")
        else:
            _delete(plan, c, have, "device_status", "device_id = ?", (d,), f"latest status of device {d} (named only by this athlete's samples)")
    plan.limitations.append("free text elsewhere (recording names, period labels, operator notes) is not searched or rewritten except as listed")
    return plan


# ---- execution --------------------------------------------------------------------------------------------------------------

def execute(path: Path, make_plan: Callable[[sqlite3.Connection], Plan], *, checkpoint: Callable[[str], None] | None = None) -> Plan:
    """Recompute the plan under an exclusive lock and apply it in that one transaction; roll back on any failure."""
    c = open_exclusive(path)          # BEGIN EXCLUSIVE is already open
    try:
        try:
            plan = make_plan(c)
            if not plan.found or plan.refusals:
                c.execute("ROLLBACK")
                return plan
            c.execute("PRAGMA secure_delete = ON")
            for d in plan.deletes:
                c.execute(f"DELETE FROM {d['table']} WHERE {d['where']}", d["params"])
                if checkpoint:
                    checkpoint(d["table"])
            for u in plan.updates:
                c.execute("UPDATE recording_imports SET period_id_map = ? WHERE recording_id = ?", (json.dumps(u["period_id_map"], sort_keys=True), u["recording_id"]))
            for d in plan.deletes:      # verify inside the transaction: everything planned is gone
                if count(c, d["table"], d["where"], d["params"]):
                    raise RuntimeError(f"rows remain in {d['table']} after deletion")
            c.execute("COMMIT")
        except BaseException:
            if c.in_transaction:
                c.execute("ROLLBACK")
            raise
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return plan
    finally:
        c.close()
