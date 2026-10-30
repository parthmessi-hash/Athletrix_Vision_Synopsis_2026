"""Receipt metadata and Server-Timing: what they mean, what they never change, and how they travel."""

import io
import json
import sqlite3
import zipfile
from datetime import UTC, datetime, timedelta

import pytest
from app.core.recording_package import FORMAT_VERSION as PV

from app.core import clock
from app.core.recording_package import PackageError, read_package
from app.db import Database, set_database
from app.models import PhysiologicalSample
from app.store.physiological import physiological_store
from app.store.recording_import import import_package

REC = "/api/v1/recordings"
T0 = datetime(2026, 10, 27, 18, 0, 0, tzinfo=UTC)


def z(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


@pytest.fixture
def clocks(monkeypatch):
    """Backend time (utc_now) and receipt time (receipt_now) controlled separately."""
    state = {"now": T0, "receipt": T0}
    monkeypatch.setattr(clock, "utc_now", lambda: state["now"])
    monkeypatch.setattr(clock, "receipt_now", lambda: state["receipt"])
    return state


def hr(client, athlete, ts: datetime, bpm):
    r = client.post("/api/v1/samples/physiological", json={"athlete_id": athlete, "timestamp": z(ts), "source": "simulated", "device_id": "sim", "heart_rate_bpm": bpm})
    assert r.status_code == 200, r.text
    return r


def test_receipt_is_metadata_not_identity_or_order(client, clocks):
    clocks["now"] = T0 + timedelta(seconds=60)
    clocks["receipt"] = T0 + timedelta(seconds=60, milliseconds=250)
    hr(client, "a1", T0 + timedelta(seconds=50), 120)
    clocks["receipt"] = T0 + timedelta(seconds=61)
    hr(client, "a1", T0 + timedelta(seconds=10), 100)          # delayed: older observation, later receipt
    live = client.get("/api/v1/athletes/a1/live").json()
    assert live["physiological"]["heart_rate_bpm"] == 120, "the newest observation stays current whatever arrived last"
    assert live["physiological_receipt"] == {"received_at": "2026-10-27T18:01:00.250000Z", "origin": "this_engine"}
    assert [s["heart_rate_bpm"] for s in client.get("/api/v1/athletes/a1/physiological/history?limit=10").json()] == [100, 120], "history ordered by observation time"
    assert client.get("/api/v1/athletes/a1/physiological/latest").json()["heart_rate_bpm"] == 120

    # equal instant: the later arrival replaces values and receipt together; the earlier receipt is not kept
    clocks["receipt"] = T0 + timedelta(seconds=62)
    hr(client, "a1", T0 + timedelta(seconds=50), None)
    live = client.get("/api/v1/athletes/a1/live").json()
    assert live["physiological"]["heart_rate_bpm"] is None and live["physiological_receipt"]["received_at"] == "2026-10-27T18:01:02Z"
    assert len(client.get("/api/v1/athletes/a1/physiological/history?limit=10").json()) == 2
    assert live["tracking"] is None and live["tracking_receipt"] is None


def test_samples_stored_without_a_request_have_no_receipt(client, clocks):
    physiological_store.add(PhysiologicalSample(athlete_id="a2", timestamp=T0, source="simulated", device_id="sim", heart_rate_bpm=0 + 60))
    live = client.get("/api/v1/athletes/a2/live").json()
    assert live["physiological"]["heart_rate_bpm"] == 60 and live["physiological_receipt"] is None, "not recorded, never invented"


def test_server_timing_and_exposed_headers(client, clocks):
    r = client.get("/api/v1/health")
    name, dur, desc = r.headers["server-timing"].split(";")
    assert name == "app" and float(dur.removeprefix("dur=")) >= 0 and "receipt to response start" in desc
    assert "access-control-expose-headers" not in r.headers, "only cross-origin requests need it"
    origin = {"Origin": "http://localhost:3000"}
    r = client.get("/api/v1/athletes/a1/live", headers=origin)
    assert r.headers["access-control-allow-origin"] == "http://localhost:3000" and r.headers["access-control-expose-headers"] == "Server-Timing"
    hr(client, "a1", T0, 80)
    r = client.get("/api/v1/athletes/a1/retained-samples.jsonl", headers=origin)
    exposed = [h.strip() for h in r.headers["access-control-expose-headers"].split(",")]
    assert exposed[0] == "Content-Disposition" and "X-Athletrix-Record-Count" in exposed and exposed[-1] == "Server-Timing", "a route's own exposed headers are kept"
    assert client.get("/api/v1/clock").json() == {"backend_time": "2026-10-27T18:00:00.000000Z"}


def test_recording_rows_carry_receipts_and_packages_carry_them_as_the_source_engines(client, clocks, tmp_path):
    for a in ("a1", "a2"):
        assert client.post("/api/v1/athletes", json={"id": a, "name": f"Fictional {a}", "jersey_number": 1, "sport": "fictional", "team_id": "t"}).status_code == 201
    rec = client.post(REC, json={"name": "timing", "athlete_ids": ["a1", "a2"]}).json()["id"]
    for s in range(5):
        clocks["now"] = clocks["receipt"] = T0 + timedelta(seconds=s + 1, milliseconds=40)
        hr(client, "a1", T0 + timedelta(seconds=s), 100 + s)
    physiological_store.add(PhysiologicalSample(athlete_id="a2", timestamp=T0, source="simulated", device_id="sim", heart_rate_bpm=70))  # rolling only, no receipt
    clocks["now"] = T0 + timedelta(seconds=10)
    client.post(f"{REC}/{rec}/stop")
    detail = client.get(f"{REC}/{rec}").json()
    assert [(e["physiological"]["sample_count"], e["physiological"]["receipt_count"]) for e in detail["recorded"]] == [(5, 5), (0, 0)]
    body = client.get(f"{REC}/{rec}/package.zip").content
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        files = {n: zf.read(n) for n in zf.namelist()}
        lines = [json.loads(x) for x in files["receipts.jsonl"].decode().splitlines()]
    assert lines[0] == {"type": "physiological", "athlete_id": "a1", "timestamp": "2026-10-27T18:00:00Z", "received_at": "2026-10-27T18:00:01.040000Z"} and len(lines) == 5
    assert all(set(json.loads(x)) == {"type", "sample"} for x in files["samples.jsonl"].decode().splitlines()), "samples.jsonl keeps the interchange format"

    dest = Database(tmp_path / "dest.db")
    try:
        new_id = import_package(dest, read_package(body), T0 + timedelta(days=1)).recording_id
        with dest.read() as c:
            rows = c.execute("SELECT ts_utc, receipt FROM recording_physiological_samples WHERE recording_id = ? ORDER BY ts_utc", (new_id,)).fetchall()
            rolling = c.execute("SELECT COUNT(*) FROM physiological_samples").fetchone()[0]
        assert [json.loads(r["receipt"]) for r in rows][0] == {"received_at": "2026-10-27T18:00:01.040000Z", "origin": "source_engine"}
        assert all(json.loads(r["receipt"])["origin"] == "source_engine" for r in rows) and rolling == 0, "imported data never looks received live here"
    finally:
        dest.close()

    # receipts that do not match a sample, or a version 1 package with receipts, are refused
    manifest = json.loads(files["manifest.json"])
    def repack(changes, version=PV):
        fs = {**files, **changes}
        m = {**manifest, "format_version": version, "members": {n: {"sha256": __import__("hashlib").sha256(fs[n]).hexdigest(), "bytes": len(fs[n])} for n in fs if n != "manifest.json"}}
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("manifest.json", json.dumps(m))
            for n in fs:
                if n != "manifest.json":
                    zf.writestr(n, fs[n])
        return buf.getvalue()
    stray = files["receipts.jsonl"] + b'{"type": "tracking", "athlete_id": "a1", "timestamp": "2026-10-27T18:00:00Z", "received_at": "2026-10-27T18:00:01Z"}\n'
    with pytest.raises(PackageError, match="no tracking sample"):
        read_package(repack({"receipts.jsonl": stray}))
    with pytest.raises(PackageError, match="not part of format version 1"):
        read_package(repack({}, version=1))


def test_older_databases_gain_the_nullable_column_and_keep_their_rows(client, clocks, tmp_path):
    path = tmp_path / "older.db"
    con = sqlite3.connect(path)   # the sample table as written before receipt metadata existed
    con.execute("CREATE TABLE physiological_samples (athlete_id TEXT NOT NULL, ts_utc TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY (athlete_id, ts_utc))")
    sample = PhysiologicalSample(athlete_id="a1", timestamp=T0 - timedelta(seconds=5), source="simulated", device_id="sim", heart_rate_bpm=88)
    con.execute("INSERT INTO physiological_samples VALUES ('a1', ?, ?)", ((T0 - timedelta(seconds=5)).isoformat(timespec="microseconds"), sample.model_dump_json()))
    con.commit()
    con.close()
    db = Database(path)
    set_database(db)
    try:
        live = client.get("/api/v1/athletes/a1/live").json()
        assert live["physiological"]["heart_rate_bpm"] == 88 and live["physiological_receipt"] is None
        clocks["receipt"] = T0
        hr(client, "a1", T0, 90)
        assert client.get("/api/v1/athletes/a1/live").json()["physiological_receipt"]["received_at"] == "2026-10-27T18:00:00Z"
    finally:
        db.close()


def test_rejected_samples_store_nothing_and_leave_existing_receipts(client, clocks):
    clocks["now"] = T0 + timedelta(seconds=10)
    clocks["receipt"] = T0 + timedelta(seconds=10, milliseconds=5)
    hr(client, "a1", T0, 100)
    first = client.get("/api/v1/athletes/a1/live").json()["physiological_receipt"]
    clocks["receipt"] = T0 + timedelta(seconds=11)
    bad = client.post("/api/v1/samples/physiological", json={"athlete_id": "a1", "timestamp": z(T0), "source": "simulated", "device_id": "sim", "heart_rate_bpm": 5})
    future = client.post("/api/v1/samples/physiological", json={"athlete_id": "a1", "timestamp": z(T0 + timedelta(seconds=30)), "source": "simulated", "device_id": "sim", "heart_rate_bpm": 120})
    assert bad.status_code == 422 and future.status_code == 422
    live = client.get("/api/v1/athletes/a1/live").json()
    assert live["physiological"]["heart_rate_bpm"] == 100 and live["physiological_receipt"] == first and live["physiological_newest_ahead_seconds"] is None
    assert len(client.get("/api/v1/athletes/a1/physiological/history?limit=10").json()) == 1
