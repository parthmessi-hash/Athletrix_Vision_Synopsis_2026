"""The configuration snapshot captured at recording start: capture, preservation, restart, packages and older data."""

import hashlib
import io
import json
import sqlite3
import zipfile
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.core import clock
from app.core.config import Settings, get_settings
from app.core.recording_package import PackageError, read_package
from app.db import Database, set_database
from app.store.recording_import import import_package

REC = "/api/v1/recordings"
T0 = datetime(2026, 10, 27, 18, 0, 0, tzinfo=UTC)
ADAPTER = {"note": "fictional wearable bridge v0.3, 1 Hz", "metadata": {"sample_rate_hz": 1, "firmware": "0.3.1", "calibrated": True, "camera": None}}


@pytest.fixture
def frozen(monkeypatch):
    state = {"now": T0}
    monkeypatch.setattr(clock, "utc_now", lambda: state["now"])
    return state


@pytest.fixture
def env(monkeypatch):
    def set_(**values):
        for k, v in values.items():
            monkeypatch.setenv(k, str(v))
        get_settings.cache_clear()
    yield set_
    get_settings.cache_clear()


def register(client, *ids):
    for n, a in enumerate(ids, start=1):
        assert client.post("/api/v1/athletes", json={"id": a, "name": f"Fictional {a}", "jersey_number": n, "sport": "fictional", "team_id": "t"}).status_code == 201


def start(client, ids, **extra):
    r = client.post(REC, json={"name": "config test", "athlete_ids": list(ids), **extra})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_snapshot_is_captured_at_start_and_kept_when_settings_change(client, frozen, env):
    env(HIGH_SPEED_THRESHOLD_MPS=4.0, TRACKING_RETENTION_SAMPLES=3000, CODE_REVISION="1bfd555")
    register(client, "a1", "a2")
    rec = start(client, ["a1", "a2"], adapter_configuration=ADAPTER)
    first = client.get(f"{REC}/{rec}").json()["configuration_at_start"]
    assert first["status"] == "captured" and first["origin"] == "this_engine"
    snap = first["snapshot"]
    assert snap["snapshot_version"] == 1 and snap["captured_at"] == "2026-10-27T18:00:00Z"
    assert snap["movement"] == {"distance_max_gap_seconds": 3.0, "distance_min_confidence": 0.5, "high_speed_threshold_mps": 4.0}
    assert snap["retention"] == {"physiological_retention_samples": 300, "tracking_retention_samples": 3000}
    assert snap["timing"] == {"max_future_timestamp_skew_seconds": 5.0}
    assert snap["effort_review"] == {"min_effort_seconds": 1.0, "max_effort_seconds": 120.0, "before_seconds": 10.0, "after_seconds": 30.0}
    assert snap["engine"] == {"application": "Athletrix Engine", "version": "0.1.0", "code_revision": "1bfd555", "package_format_version": 3}
    assert snap["operator_supplied"] == {**ADAPTER, "verified_by_engine": False}
    text = json.dumps(snap)
    for forbidden in ("database_path", "athletrix.db", "cors", "localhost", "/Users/", "/private/", "host", "port"):
        assert forbidden not in text.lower(), forbidden

    # settings change while the recording runs and after it stops: the snapshot is what was in force at the start
    env(HIGH_SPEED_THRESHOLD_MPS=6.5, TRACKING_RETENTION_SAMPLES=300, CODE_REVISION="feedbee")
    frozen["now"] = T0 + timedelta(seconds=30)
    client.post(f"{REC}/{rec}/stop")
    assert client.get(f"{REC}/{rec}").json()["configuration_at_start"] == first
    rec2 = start(client, ["a1"])
    second = client.get(f"{REC}/{rec2}").json()["configuration_at_start"]["snapshot"]
    assert second["movement"]["high_speed_threshold_mps"] == 6.5 and second["engine"]["code_revision"] == "feedbee" and second["operator_supplied"] is None


def test_snapshot_survives_a_restart(client, frozen, isolated_db, tmp_path):
    register(client, "a1")
    rec = start(client, ["a1"])
    before = client.get(f"{REC}/{rec}").json()
    isolated_db.close()
    reopened = Database(tmp_path / "test.db")
    set_database(reopened)
    try:
        after = client.get(f"{REC}/{rec}").json()
        assert after["configuration_at_start"] == before["configuration_at_start"] and after["status"] == "active"
    finally:
        reopened.close()


def test_unknown_code_revision_and_bounded_adapter_input(client, frozen):
    register(client, "a1")
    rec = start(client, ["a1"], adapter_configuration={"note": "   "})
    snap = client.get(f"{REC}/{rec}").json()["configuration_at_start"]["snapshot"]
    assert snap["engine"]["code_revision"] is None and snap["operator_supplied"] is None, "nothing supplied: unknown and absent, never guessed"
    client.post(f"{REC}/{rec}/stop")
    too_many = {"metadata": {f"k{n}": n for n in range(33)}}
    for bad in (too_many, {"note": "x" * 501}, {"metadata": {"nested": {"a": 1}}}, {"metadata": {"k": "v" * 201}}, {"metadata": {" ": 1}}, {"unexpected": 1}):
        r = client.post(REC, json={"name": "bad", "athlete_ids": ["a1"], "adapter_configuration": bad})
        assert r.status_code == 422, bad
    assert len(client.get(REC).json()) == 1, "a rejected start creates nothing"
    with pytest.raises(ValidationError):
        Settings(code_revision="not a revision; rm -rf")
    assert Settings(code_revision="  ").code_revision is None


def repack(files: dict[str, bytes], version: int) -> bytes:
    manifest = json.loads(files["manifest.json"])
    manifest["format_version"] = version
    names = ("recording.json", "samples.jsonl", "periods.json") + (("receipts.jsonl",) if version >= 2 else ())
    manifest["members"] = {name: {"sha256": hashlib.sha256(files[name]).hexdigest(), "bytes": len(files[name])} for name in names}
    if version < 2:
        manifest["counts"].pop("receipts", None)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("manifest.json", json.dumps(manifest))
        for name in names:
            z.writestr(name, files[name])
    return buf.getvalue()


def test_package_round_trip_keeps_the_start_snapshot_apart_from_export_settings(client, frozen, env, tmp_path):
    env(HIGH_SPEED_THRESHOLD_MPS=4.0)
    register(client, "a1")
    rec = start(client, ["a1"], adapter_configuration=ADAPTER)
    frozen["now"] = T0 + timedelta(seconds=10)
    client.post(f"{REC}/{rec}/stop")
    original = client.get(f"{REC}/{rec}").json()["configuration_at_start"]
    env(HIGH_SPEED_THRESHOLD_MPS=7.0)
    body = client.get(f"{REC}/{rec}/package.zip").content
    with zipfile.ZipFile(io.BytesIO(body)) as z:
        files = {n: z.read(n) for n in z.namelist()}
    assert json.loads(files["manifest.json"])["settings_at_export"]["high_speed_threshold_mps"] == 7.0
    assert json.loads(files["recording.json"])["configuration_at_start"] == original["snapshot"]

    dest = Database(tmp_path / "dest.db")
    try:
        new_id = import_package(dest, read_package(body), T0 + timedelta(days=1)).recording_id
        set_database(dest)
        imported = client.get(f"{REC}/{new_id}").json()["configuration_at_start"]
        assert imported["status"] == "captured" and imported["origin"] == "source_engine" and imported["snapshot"] == original["snapshot"]
        assert "carried in the imported package" in imported["note"]
        # re-exported from the destination, it is still the original start-time snapshot
        again = client.get(f"{REC}/{new_id}/package.zip").content
        with zipfile.ZipFile(io.BytesIO(again)) as z:
            assert json.loads(z.read("recording.json"))["configuration_at_start"] == original["snapshot"]
    finally:
        dest.close()

    # a version 1 package (no snapshot key) imports as "unavailable"; a version 1 package claiming one is rejected
    rec_doc = json.loads(files["recording.json"])
    v1 = repack({**files, "recording.json": json.dumps({k: v for k, v in rec_doc.items() if k != "configuration_at_start"}).encode()}, 1)
    assert read_package(v1).configuration is None and read_package(v1).summary()["configuration_at_start"]["status"] == "unavailable"
    with pytest.raises(PackageError, match="not part of format version 1"):
        read_package(repack(files, 1))
    tampered = {**rec_doc, "configuration_at_start": {**rec_doc["configuration_at_start"], "movement": {"distance_max_gap_seconds": "wide"}}}
    with pytest.raises(PackageError, match="configuration_at_start is invalid"):
        read_package(repack({**files, "recording.json": json.dumps(tampered).encode()}, 2))
    dest_v1 = Database(tmp_path / "dest-v1.db")
    try:
        v1_id = import_package(dest_v1, read_package(v1), T0 + timedelta(days=1)).recording_id
        set_database(dest_v1)
        cfg = client.get(f"{REC}/{v1_id}").json()["configuration_at_start"]
        assert cfg == {"status": "unavailable", "origin": None, "snapshot": None, "note": cfg["note"]} and "unknown" in cfg["note"]
    finally:
        dest_v1.close()


def test_recordings_in_an_older_database_report_the_snapshot_as_unavailable(client, tmp_path):
    path = tmp_path / "older.db"
    con = sqlite3.connect(path)   # the recording tables as written before snapshots existed (no recording_configurations)
    con.executescript("""
        CREATE TABLE recordings (id TEXT PRIMARY KEY, name TEXT NOT NULL, started_at TEXT NOT NULL, stopped_at TEXT);
        CREATE TABLE recording_athletes (recording_id TEXT NOT NULL, athlete_id TEXT NOT NULL, name TEXT NOT NULL, jersey_number INTEGER NOT NULL, sport TEXT NOT NULL, team_id TEXT NOT NULL, PRIMARY KEY (recording_id, athlete_id));
        INSERT INTO recordings VALUES ('rec-old', 'older', '2026-10-20T10:00:00.000000+00:00', '2026-10-20T10:05:00.000000+00:00');
        INSERT INTO recording_athletes VALUES ('rec-old', 'a1', 'Fictional a1', 1, 'fictional', 't');
    """)
    con.close()
    db = Database(path)
    set_database(db)
    try:
        detail = client.get(f"{REC}/rec-old").json()
        assert detail["configuration_at_start"]["status"] == "unavailable" and detail["configuration_at_start"]["snapshot"] is None
        assert client.get(f"{REC}/rec-old/package.zip").status_code == 200
    finally:
        db.close()
