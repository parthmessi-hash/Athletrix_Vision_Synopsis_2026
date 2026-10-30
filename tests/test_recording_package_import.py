"""scripts/recording_package.py: inspect, dry-run and atomic import of portable recording packages. All data is fictional."""

import hashlib
import importlib.util
import io
import json
import sqlite3
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from app.core.recording_package import FORMAT_VERSION as PV
from fastapi.testclient import TestClient

from app.core import clock
from app.db import Database, set_database
from app.main import create_app

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("recording_package_cli", ROOT / "scripts" / "recording_package.py")
cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)

NOW = datetime(2026, 10, 26, 20, 0, 0, tzinfo=UTC)
REC = "/api/v1/recordings"


def at(s):
    return NOW + timedelta(seconds=s)


def z(s):
    return at(s).isoformat().replace("+00:00", "Z")


@pytest.fixture
def frozen(monkeypatch):
    state = {"now": NOW}
    monkeypatch.setattr(clock, "utc_now", lambda: state["now"])
    return state


@pytest.fixture
def package(client, frozen, tmp_path):
    """A stopped two-athlete recording with nulls, zeros, an offset timestamp and two saved periods (same label), as a package file."""
    for n, a in enumerate(("a1", "a2"), start=1):
        assert client.post("/api/v1/athletes", json={"id": a, "name": f"Fictional {a}", "jersey_number": n, "sport": "fictional", "team_id": "t"}).status_code == 201
    rec = client.post(REC, json={"name": "Tuesday practice", "athlete_ids": ["a1", "a2"]}).json()["id"]
    for s in range(12):
        frozen["now"] = at(s)
        for a in ("a1", "a2"):
            ts = at(s).astimezone(__import__("datetime").timezone(timedelta(hours=2))).isoformat() if (a == "a1" and s == 3) else z(s)
            assert client.post("/api/v1/samples/physiological", json={"athlete_id": a, "timestamp": ts, "source": "wearable", "device_id": f"band-{a}", "heart_rate_bpm": None if s == 4 else 100 + s, "spo2_percent": 97}).status_code == 200
            assert client.post("/api/v1/samples/tracking", json={"athlete_id": a, "timestamp": z(s), "x_position": float(s), "y_position": 0.0, "speed_mps": 0.0 if s == 0 else 2.0 * s, "acceleration_mps2": 0.0, "tracking_confidence": 0.3 if s == 6 else 1.0}).status_code == 200
    frozen["now"] = at(20)
    client.post(f"{REC}/{rec}/stop")
    for a, b in ((1, 5), (6, 10)):
        assert client.post(f"{REC}/{rec}/athletes/a1/periods", json={"label": "sprint", "effort_start": z(a), "effort_end": z(b)}).status_code == 201
    r = client.get(f"{REC}/{rec}/package.zip")
    path = tmp_path / "pkg.zip"
    path.write_bytes(r.content)
    return {"rec": rec, "path": path, "bytes": r.content}


def run(argv):
    lines = []
    code = cli.main(argv, out=lines.append, now=lambda: datetime(2026, 10, 28, 9, 0, 0, tzinfo=UTC))
    return code, "\n".join(lines)


def files_of(data):
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {n: zf.read(n) for n in zf.namelist()}


def repack(files, fix=True, extra=None):
    files = dict(files)
    if fix:
        m = json.loads(files["manifest.json"])
        for n in ("recording.json", "samples.jsonl", "periods.json"):
            if n in files:
                m["members"][n] = {"sha256": hashlib.sha256(files[n]).hexdigest(), "bytes": len(files[n])}
        files["manifest.json"] = json.dumps(m).encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for n, b in {**files, **(extra or {})}.items():
            zf.writestr(n, b)
    return buf.getvalue()


def dest_client(path):
    db = Database(path); set_database(db)
    return db, TestClient(create_app())


def test_inspect_and_dry_run_write_nothing(package, tmp_path):
    code, text = run(["inspect", str(package["path"])])
    assert code == 0 and "VALID PACKAGE" in text and "2 athletes · 24 physiological and 24 tracking samples · 2 saved periods" in text and f"athletrix-recording-package version {PV}" in text and "Configuration at recording start (captured by the source engine)" in text
    code, js = run(["inspect", str(package["path"]), "--json"])
    s = json.loads(js)
    assert s["athletes"][0]["physiological"] == {"count": 12, "bounds": {"earliest": z(0), "latest": z(11)}} and s["recording"]["started_at"] == "2026-10-26T20:00:00Z"
    missing = tmp_path / "dest.db"
    code, text = run(["import", str(package["path"]), "--database", str(missing), "--dry-run"])
    assert code == 0 and "nothing was written" in text and not missing.exists(), "a dry run creates no file"
    existing = tmp_path / "existing.db"
    db = Database(existing); db.close()
    before = existing.read_bytes()
    code, text = run(["import", str(package["path"]), "--database", str(existing), "--dry-run"])
    assert code == 0 and existing.read_bytes() == before, "a dry run leaves an existing destination byte-for-byte unchanged"


def test_import_creates_a_new_stopped_recording_with_provenance_and_touches_nothing_else(package, tmp_path, frozen):
    dest = tmp_path / "dest.db"
    db, c = dest_client(dest)
    assert c.post("/api/v1/athletes", json={"id": "z9", "name": "Other", "jersey_number": 9, "sport": "fictional", "team_id": "u"}).status_code == 201
    frozen["now"] = datetime(2026, 10, 28, 8, 0, 0, tzinfo=UTC)
    active = c.post(REC, json={"name": "running here", "athlete_ids": ["z9"]}).json()["id"]
    assert c.post("/api/v1/samples/tracking", json={"athlete_id": "a1", "timestamp": "2026-10-28T08:00:00Z", "x_position": 0, "y_position": 0, "speed_mps": 1.0, "acceleration_mps2": 0, "tracking_confidence": 1}).status_code == 200
    db.close(); set_database(None)
    code, text = run(["import", str(package["path"]), "--database", str(dest), "--json"])
    assert code == 0, text
    res = json.loads(text)
    new_id = res["recording_id"]
    assert new_id != package["rec"] and res["source_recording_id"] == package["rec"] and res["samples"] == {"physiological": 24, "tracking": 24}
    assert set(res["period_id_map"]) == {p["id"] for p in json.loads(files_of(package["bytes"])["periods.json"])} and len(set(res["period_id_map"].values())) == 2
    db, c = dest_client(dest)   # a restart of the destination
    try:
        detail = c.get(f"{REC}/{new_id}").json()
        assert detail["status"] == "stopped" and detail["started_at"] == "2026-10-26T20:00:00Z" and detail["stopped_at"] == "2026-10-26T20:00:20Z" and detail["name"] == "Tuesday practice"
        assert [a["id"] for a in detail["athletes"]] == ["a1", "a2"] and detail["athletes"][0]["name"] == "Fictional a1"
        assert detail["imported"]["source_recording_id"] == package["rec"] and detail["imported"]["period_id_map"] == res["period_id_map"] and detail["imported"]["package_format"] == f"athletrix-recording-package/{PV}"
        assert detail["configuration_at_start"]["status"] == "captured" and detail["configuration_at_start"]["origin"] == "source_engine" and detail["configuration_at_start"]["snapshot"]["captured_at"] == "2026-10-26T20:00:00Z"
        # every sample exactly as in the package (offsets, nulls, zeros, confidence)
        exported = files_of(package["bytes"])["samples.jsonl"].decode().splitlines()
        again = c.get(f"{REC}/{new_id}/athletes/a1/recorded-samples.jsonl").text.splitlines() + c.get(f"{REC}/{new_id}/athletes/a2/recorded-samples.jsonl").text.splitlines()
        assert again == exported
        periods = c.get(f"{REC}/{new_id}/athletes/a1/periods").json()
        assert [(p["label"], p["effort_start"], p["effort_end"]) for p in periods] == [("sprint", z(1), z(5)), ("sprint", z(6), z(10))] and {p["id"] for p in periods} == set(res["period_id_map"].values())
        # nothing else changed: registry, rolling history, the active recording
        assert [a["id"] for a in c.get("/api/v1/athletes").json()] == ["z9"], "the registry is untouched"
        assert len(c.get("/api/v1/athletes/a1/tracking/history").json()) == 1 and c.get("/api/v1/athletes/a2/retained-data").json()["physiological"]["sample_count"] == 0, "rolling history is untouched"
        assert c.get(f"{REC}/active").json()["recording"]["id"] == active
        assert c.get(f"{REC}/{active}").json()["imported"] is None
    finally:
        db.close(); set_database(None)
    # repeated import: refused, unless explicitly allowed (then a separate copy)
    code, text = run(["import", str(package["path"]), "--database", str(dest)])
    assert code == 1 and "REFUSED" in text and new_id in text
    code, text = run(["import", str(package["path"]), "--database", str(dest), "--dry-run"])
    assert code == 1 and "would be REFUSED" in text
    code, text = run(["import", str(package["path"]), "--database", str(dest), "--allow-duplicate", "--json"])
    second = json.loads(text)["recording_id"]
    assert code == 0 and second != new_id
    con = sqlite3.connect(dest)
    assert con.execute("SELECT COUNT(*) FROM recording_imports").fetchone()[0] == 2 and con.execute("SELECT COUNT(*) FROM recording_physiological_samples").fetchone()[0] == 48
    con.close()


def test_forced_failure_mid_import_rolls_back_everything(package, tmp_path):
    dest = tmp_path / "dest.db"
    for stage in ("recording", "samples:a2:tracking", "periods", "provenance"):
        def boom(s, stage=stage):
            if s == stage:
                raise RuntimeError(f"forced failure at {stage}")
        lines = []
        code = cli.main(["import", str(package["path"]), "--database", str(dest)], out=lines.append, checkpoint=boom)
        assert code == 1 and "rolled back completely" in lines[-1], stage
        con = sqlite3.connect(dest)
        for table in ("recordings", "recording_athletes", "recording_physiological_samples", "recording_tracking_samples", "recording_periods", "recording_imports"):
            assert con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, (stage, table)
        con.close()
    assert run(["import", str(package["path"]), "--database", str(dest)])[0] == 0, "after the failures a clean import works and is not taken for a repeat"


def corrupt_cases(files):
    lines = files["samples.jsonl"].decode().splitlines()
    first = json.loads(lines[0])
    conflict = json.loads(lines[0]); conflict["sample"]["spo2_percent"] = 99
    rec = json.loads(files["recording.json"])
    periods = json.loads(files["periods.json"])
    manifest = json.loads(files["manifest.json"])
    late = json.loads(lines[0]); late["sample"]["timestamp"] = "2026-10-26T20:00:26Z"
    return {
        "checksum": (repack({**files, "samples.jsonl": files["samples.jsonl"].replace(b"band-a1", b"band-xx", 1)}, fix=False), "checksum or size does not match"),
        "malformed line": (repack({**files, "samples.jsonl": files["samples.jsonl"] + b"{not json\n"}), "not valid JSON"),
        "invalid sample": (repack({**files, "samples.jsonl": files["samples.jsonl"] + (json.dumps({"type": "tracking", "sample": {**first["sample"], "tracking_confidence": 2}}) + "\n").encode()}), "invalid tracking sample"),
        "conflicting duplicate": (repack({**files, "samples.jsonl": files["samples.jsonl"] + (json.dumps(conflict) + "\n").encode()}), "conflicting duplicate"),
        "unknown athlete": (repack({**files, "samples.jsonl": files["samples.jsonl"] + (json.dumps({"type": "physiological", "sample": {**first["sample"], "athlete_id": "intruder"}}) + "\n").encode()}), "not in the recording's roster"),
        "after stop": (repack({**files, "samples.jsonl": files["samples.jsonl"] + (json.dumps(late) + "\n").encode()}), "after the recording stopped"),
        "count mismatch": (repack({**files, "manifest.json": json.dumps({**manifest, "counts": {**manifest["counts"], "tracking_samples": 23}}).encode()}), "counts do not match"),
        "version": (repack({**files, "manifest.json": json.dumps({**manifest, "format_version": PV + 1}).encode()}), f"unsupported format_version {PV + 1}"),
        "missing member": (repack({k: v for k, v in files.items() if k != "periods.json"}), "missing required members: periods.json"),
        "path member": (repack(files, extra={"../evil.sh": b"echo"}), "unexpected members"),
        "period athlete": (repack({**files, "periods.json": json.dumps([{**periods[0], "athlete_id": "intruder"}, periods[1]]).encode()}), "is not in the recording's roster"),
        "period bounds": (repack({**files, "periods.json": json.dumps([{**periods[0], "effort_end": periods[0]["effort_start"]}, periods[1]]).encode()}), "must be between 1 and 120 s"),
        "active status": (repack({**files, "recording.json": json.dumps({**rec, "status": "active"}).encode()}), "only stopped recordings"),
        "not a zip": (b"PK\x03\x04 broken", "not a readable ZIP"),
    }


def test_corrupt_and_invalid_packages_are_rejected_before_anything_is_written(package, tmp_path):
    dest = tmp_path / "dest.db"
    for name, (data, needle) in corrupt_cases(files_of(package["bytes"])).items():
        p = tmp_path / f"{name.replace(' ', '_')}.zip"
        p.write_bytes(data)
        code, text = run(["import", str(p), "--database", str(dest)])
        assert code == 1 and "INVALID PACKAGE" in text and needle in text, (name, text)
        assert not dest.exists(), f"{name}: the destination was not even created"
    assert not (tmp_path / "evil.sh").exists() and not (tmp_path.parent / "evil.sh").exists(), "nothing is extracted"
    assert run(["import", str(tmp_path / "nope.zip"), "--database", str(dest)])[0] == 1
    assert run(["import", str(package["path"])])[0] == 2, "--database is required"
