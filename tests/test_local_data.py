"""scripts/local_data.py: inventory, dry runs and scoped removal on fictional temporary databases only."""

import hashlib
import importlib.util
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.core import clock
from app.core.recording_package import read_package
from app.db import Database, set_database
from app.store.recording_import import import_package

spec = importlib.util.spec_from_file_location("local_data_cli", Path(__file__).resolve().parents[1] / "scripts" / "local_data.py")
local_data = importlib.util.module_from_spec(spec); spec.loader.exec_module(local_data)

REC = "/api/v1/recordings"
T0 = datetime(2026, 10, 27, 18, 0, 0, tzinfo=UTC)


def z(dt):
    return dt.isoformat().replace("+00:00", "Z")


def files(path):
    """Every byte of the database and its sidecars (a dry run must leave all of them as they were)."""
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(path.parent.glob(path.name + "*"))}


def dump(path):
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return sorted(c.iterdump())
    finally:
        c.close()


def rows(path, sql, *params):
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return c.execute(sql, params).fetchall()
    finally:
        c.close()


def run(*argv, checkpoint=None):
    lines = []
    code = local_data.main([str(a) for a in argv], out=lines.append, checkpoint=checkpoint)
    return code, "\n".join(lines)


@pytest.fixture
def world(client, monkeypatch, isolated_db, tmp_path):
    """Three fictional athletes: R1 shared by a1 and a2, R2 only a1, R3 only a2, R1i an imported copy of R1; then the engine is 'stopped'."""
    state = {"now": T0}
    monkeypatch.setattr(clock, "utc_now", lambda: state["now"])
    for n, a in enumerate(("a1", "a2", "a3"), start=1):
        assert client.post("/api/v1/athletes", json={"id": a, "name": f"Fictional {a}", "jersey_number": n, "sport": "fictional", "team_id": "t"}).status_code == 201

    def feed(athletes, seconds, offset):
        for s in range(seconds):
            state["now"] = T0 + timedelta(seconds=offset + s + 1)
            for a in athletes:
                ts = z(T0 + timedelta(seconds=offset + s))
                dev = {"a1": "w-1", "a2": "w-2", "a3": "w-3"}[a]
                assert client.post("/api/v1/samples/physiological", json={"athlete_id": a, "timestamp": ts, "source": "simulated", "device_id": dev, "heart_rate_bpm": None if s == 0 else 100 + s}).status_code == 200
                assert client.post("/api/v1/samples/tracking", json={"athlete_id": a, "timestamp": ts, "x_position": 0, "y_position": 0, "speed_mps": 0.0 if s == 1 else 4.0 + s % 3, "acceleration_mps2": 0, "tracking_confidence": 1, "device_id": "cam"}).status_code == 200

    def recording(name, athletes, offset):
        state["now"] = T0 + timedelta(seconds=offset)
        rec = client.post(REC, json={"name": name, "athlete_ids": athletes, "adapter_configuration": {"note": "fictional bridge"}}).json()["id"]
        feed(athletes, 30, offset)
        state["now"] = T0 + timedelta(seconds=offset + 31)
        client.post(f"{REC}/{rec}/stop")
        state["now"] = T0 + timedelta(seconds=offset + 40)
        for a in athletes:
            assert client.post(f"{REC}/{rec}/athletes/{a}/periods", json={"label": f"{a} effort", "effort_start": z(T0 + timedelta(seconds=offset + 5)), "effort_end": z(T0 + timedelta(seconds=offset + 15))}).status_code == 201
        return rec

    r1 = recording("shared", ["a1", "a2"], 0)
    r2 = recording("a1 only", ["a1"], 100)
    r3 = recording("a2 only", ["a2"], 200)
    feed(["a3"], 5, 300)
    for d in ("w-1", "w-2", "cam"):
        assert client.post("/api/v1/devices/status", json={"device_id": d, "timestamp": z(T0), "connected": True, "battery_percent": 0}).status_code == 200
    r1i = import_package(isolated_db, read_package(client.get(f"{REC}/{r1}/package.zip").content), T0 + timedelta(hours=1), allow_duplicate=True).recording_id
    state["now"] = T0 + timedelta(hours=2)
    reviews = {(r, a): client.get(f"{REC}/{r}/athletes/{a}/period-reviews").json()["items"] for r, a in ((r1, "a2"), (r3, "a2"), (r1i, "a2"))}
    path = tmp_path / "test.db"
    isolated_db.close()   # the engine is stopped before the tool runs
    yield {"path": path, "r1": r1, "r2": r2, "r3": r3, "r1i": r1i, "reviews": reviews, "state": state}


def reopen_reviews(client, w):
    db = Database(w["path"])
    set_database(db)
    try:
        return {k: client.get(f"{REC}/{k[0]}/athletes/{k[1]}/period-reviews").json()["items"] for k in w["reviews"]}
    finally:
        db.close()


def test_inventory_and_dry_runs_change_nothing(world):
    path = world["path"]
    before = files(path)
    code, text = run("inventory", "--database", path)
    assert code == 0 and "recording_athletes" in text and "athlete a1 (registered)" in text
    code, text = run("remove-athlete", "a1", "--database", path, "--json")
    plan = json.loads(text)
    assert code == 0 and plan["executable"] and not plan["executed"]
    by_table = {}
    for d in plan["deletes"]:
        by_table[d["table"]] = by_table.get(d["table"], 0) + d["count"]
    assert by_table["athletes"] == 1 and by_table["physiological_samples"] == 60 and by_table["recordings"] == 1, "R2 had only a1"
    assert by_table["recording_periods"] == 3 + 0 and by_table["recording_physiological_samples"] == 90, "R1, R2 and the imported R1 copy"
    assert any("recording_imports" == u["table"] for u in plan["updates"])
    assert any("stays valid for a2" in k for k in plan["kept"]) and any("w-1" in k and "kept" in k for k in plan["kept"])
    assert any("portable package" in lim for lim in plan["limitations"]) and plan["cannot_remove"]
    code, text = run("remove-recording", world["r1"], "--database", path)
    assert code == 0 and text.startswith("DRY RUN (nothing was changed)")
    after = files(path)
    assert after["test.db"] == before["test.db"], "inventory and dry runs leave every byte of the database file as it was"
    assert set(after) - set(before) <= {"test.db-wal", "test.db-shm"} and (path.parent / "test.db-wal").stat().st_size == 0, \
        "SQLite may create empty WAL and shared-memory sidecars when it opens a WAL database read-only; they hold no data"
    assert not (path.parent / "missing.db").exists() and run("inventory", "--database", path.parent / "missing.db")[0] == 1
    assert not (path.parent / "missing.db").exists(), "a missing database is never created"


def test_execution_needs_the_exact_id_and_unknown_targets_change_nothing(world):
    path = world["path"]
    before = dump(path)
    assert run("remove-athlete", "a1", "--database", path, "--execute")[0] == 2
    assert run("remove-athlete", "a1", "--database", path, "--execute", "--confirm", "a2")[0] == 2
    code, text = run("remove-athlete", "zz", "--database", path, "--execute", "--confirm", "zz")
    assert code == 1 and "No athlete 'zz' is stored" in text
    code, text = run("remove-recording", "rec-nope", "--database", path, "--execute", "--confirm", "rec-nope")
    assert code == 1 and "nothing was changed" in text
    assert dump(path) == before


def test_removing_one_athlete_keeps_the_others_and_their_reviews(client, world):
    path, r1, r2, r3, r1i = world["path"], world["r1"], world["r2"], world["r3"], world["r1i"]
    other_before = rows(path, "SELECT * FROM physiological_samples WHERE athlete_id != 'a1' ORDER BY athlete_id, ts_utc")
    code, text = run("remove-athlete", "a1", "--database", path, "--include-device-status", "cam", "--execute", "--confirm", "a1")
    assert code == 1 and "REFUSED: device cam: other athletes' stored samples also name it" in text, "a shared device blocks the whole removal"
    code, text = run("remove-athlete", "a1", "--database", path, "--include-device-status", "w-1", "--execute", "--confirm", "a1")
    assert code == 0 and text.startswith("REMOVED: athlete a1"), text
    for table in ("physiological_samples", "tracking_samples", "recording_athletes", "recording_physiological_samples", "recording_tracking_samples", "recording_periods"):
        assert rows(path, f"SELECT COUNT(*) FROM {table} WHERE athlete_id = 'a1'")[0][0] == 0, table
    assert rows(path, "SELECT COUNT(*) FROM athletes WHERE id = 'a1'")[0][0] == 0
    assert [r[0] for r in rows(path, "SELECT id FROM recordings ORDER BY id")] == sorted([r1, r3, r1i]), "R2 (only a1) is gone, the shared ones stay"
    assert rows(path, "SELECT COUNT(*) FROM recording_configurations WHERE recording_id = ?", r2)[0][0] == 0
    assert [r[0] for r in rows(path, "SELECT athlete_id FROM recording_athletes WHERE recording_id = ?", r1)] == ["a2"]
    assert rows(path, "SELECT * FROM physiological_samples WHERE athlete_id != 'a1' ORDER BY athlete_id, ts_utc") == other_before
    assert [r[0] for r in rows(path, "SELECT device_id FROM device_status ORDER BY device_id")] == ["cam", "w-2"], "only the named, unshared device status went"
    mapping = json.loads(rows(path, "SELECT period_id_map FROM recording_imports WHERE recording_id = ?", r1i)[0][0])
    kept_ids = {r[0] for r in rows(path, "SELECT id FROM recording_periods WHERE recording_id = ?", r1i)}
    assert set(mapping.values()) == kept_ids and len(mapping) == 1, "the import mapping keeps only periods that still exist"
    assert reopen_reviews(client, world) == world["reviews"], "a2's reviews are identical after a1's removal"
    assert not list(path.parent.glob("test.db-wal")) or (path.parent / "test.db-wal").stat().st_size == 0


def test_removing_a_recording_keeps_rolling_data_and_other_recordings(client, world):
    path, r1 = world["path"], world["r1"]
    keep = {t: rows(path, f"SELECT * FROM {t} ORDER BY 1, 2") for t in ("athletes", "physiological_samples", "tracking_samples", "device_status")}
    others = rows(path, "SELECT * FROM recording_physiological_samples WHERE recording_id != ? ORDER BY 1, 2, 3", r1)
    code, text = run("remove-recording", r1, "--database", path, "--execute", "--confirm", r1)
    assert code == 0, text
    for t in ("recordings", "recording_athletes", "recording_physiological_samples", "recording_tracking_samples", "recording_periods", "recording_configurations", "recording_imports"):
        col = "id" if t == "recordings" else "recording_id"
        assert rows(path, f"SELECT COUNT(*) FROM {t} WHERE {col} = ?", r1)[0][0] == 0, t
    assert {t: rows(path, f"SELECT * FROM {t} ORDER BY 1, 2") for t in keep} == keep
    assert rows(path, "SELECT * FROM recording_physiological_samples WHERE recording_id != ? ORDER BY 1, 2, 3", r1) == others
    after = reopen_reviews(client, {**world, "reviews": {k: v for k, v in world["reviews"].items() if k[0] != r1}})
    assert after == {k: v for k, v in world["reviews"].items() if k[0] != r1}, "the imported copy and R3 still review identically"


def test_a_failure_partway_rolls_everything_back(world):
    path = world["path"]
    before = dump(path)
    calls = []
    def boom(stage):
        calls.append(stage)
        if len(calls) == 3:
            raise RuntimeError("simulated failure")
    code, text = run("remove-athlete", "a1", "--database", path, "--execute", "--confirm", "a1", checkpoint=boom)
    assert code == 1 and "rolled back completely" in text and len(calls) == 3
    assert dump(path) == before


def test_concurrent_access_is_refused_by_both_layers(world):
    path, r1 = world["path"], world["r1"]
    before = dump(path)
    engine = Database(path)   # an engine of this version: holds the shared maintenance lease while open
    try:
        code, text = run("remove-recording", r1, "--database", path, "--execute", "--confirm", r1)
        assert code == 1 and "holds its lease" in text and "nothing was changed" in text
    finally:
        engine.close()
    older = sqlite3.connect(path)   # a client outside the lease (an older engine, a shell) that has read the file
    older.execute("SELECT COUNT(*) FROM recordings").fetchone()
    try:
        code, text = run("remove-recording", r1, "--database", path, "--execute", "--confirm", r1)
        assert code == 1 and "another connection has it open" in text, "the SQLite layer still catches it"
    finally:
        older.close()
    from app.core.db_lease import Lease, LeaseUnavailable
    with Lease(path, exclusive=True):   # a maintenance tool at work: the engine refuses to open the file
        with pytest.raises(LeaseUnavailable, match="maintenance tool"):
            Database(path)
    assert dump(path) == before


def test_a_real_running_engine_blocks_removal_and_import(world, tmp_path, client):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from validate_team_scale import IsolatedBackend
    spec = importlib.util.spec_from_file_location("recording_package_cli2", Path(__file__).resolve().parents[1] / "scripts" / "recording_package.py")
    pkg_cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(pkg_cli)
    db = Database(world["path"]); set_database(db)
    try:
        package = tmp_path / "r1.zip"
        package.write_bytes(client.get(f"{REC}/{world['r1']}/package.zip").content)
    finally:
        db.close()
    backend = IsolatedBackend()
    try:
        import shutil
        shutil.copy(world["path"], backend.db_path)
        backend.start()
        before = dump(backend.db_path)
        code, text = run("remove-athlete", "a1", "--database", backend.db_path, "--execute", "--confirm", "a1")
        assert code == 1 and "REFUSED" in text and "nothing was changed" in text
        lines = []
        assert pkg_cli.main(["import", str(package), "--database", str(backend.db_path), "--allow-duplicate"], out=lines.append) == 1 and "REFUSED" in lines[0]
        assert dump(backend.db_path) == before
        backend.stop()
        code, text = run("remove-athlete", "a1", "--database", backend.db_path, "--execute", "--confirm", "a1")
        assert code == 0, "once the engine has stopped, removal proceeds"
    finally:
        backend.cleanup()


def test_active_recordings_are_refused(client, world):
    db = Database(world["path"])
    set_database(db)
    try:
        rec = client.post(REC, json={"name": "running", "athlete_ids": ["a3"]}).json()["id"]
    finally:
        db.close()
    code, text = run("remove-recording", rec, "--database", world["path"], "--execute", "--confirm", rec)
    assert code == 1 and "is active; stop it first" in text
    code, text = run("remove-athlete", "a3", "--database", world["path"], "--execute", "--confirm", "a3")
    assert code == 1 and "in active recording" in text


def test_an_older_database_without_newer_tables_can_be_inventoried_and_cleaned(tmp_path):
    path = tmp_path / "older.db"
    c = sqlite3.connect(path)   # recording tables as written before snapshots, receipts and imports existed
    c.executescript("""
        CREATE TABLE athletes (id TEXT PRIMARY KEY, name TEXT NOT NULL, jersey_number INTEGER NOT NULL, sport TEXT NOT NULL, team_id TEXT NOT NULL);
        CREATE TABLE physiological_samples (athlete_id TEXT NOT NULL, ts_utc TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY (athlete_id, ts_utc));
        CREATE TABLE recordings (id TEXT PRIMARY KEY, name TEXT NOT NULL, started_at TEXT NOT NULL, stopped_at TEXT);
        CREATE TABLE recording_athletes (recording_id TEXT NOT NULL, athlete_id TEXT NOT NULL, name TEXT NOT NULL, jersey_number INTEGER NOT NULL, sport TEXT NOT NULL, team_id TEXT NOT NULL, PRIMARY KEY (recording_id, athlete_id));
        INSERT INTO athletes VALUES ('o1', 'Older one', 1, 'fictional', 't'), ('o2', 'Older two', 2, 'fictional', 't');
        INSERT INTO physiological_samples VALUES ('o1', '2026-10-20T10:00:00.000000+00:00', '{"athlete_id": "o1", "device_id": "w"}');
        INSERT INTO recordings VALUES ('rec-old', 'older', '2026-10-20T10:00:00.000000+00:00', '2026-10-20T10:05:00.000000+00:00');
        INSERT INTO recording_athletes VALUES ('rec-old', 'o1', 'Older one', 1, 'fictional', 't'), ('rec-old', 'o2', 'Older two', 2, 'fictional', 't');
    """)
    c.close()
    assert run("inventory", "--database", path)[0] == 0
    code, text = run("remove-athlete", "o1", "--database", path, "--execute", "--confirm", "o1")
    assert code == 0, text
    assert rows(path, "SELECT athlete_id FROM recording_athletes") == [("o2",)] and rows(path, "SELECT id FROM athletes") == [("o2",)]
    assert {r[0] for r in rows(path, "SELECT name FROM sqlite_master WHERE type = 'table'")} == {"athletes", "physiological_samples", "recordings", "recording_athletes"}, "no schema is added"
