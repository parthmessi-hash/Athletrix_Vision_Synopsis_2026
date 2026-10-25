"""SQLite persistence: data survives reopening the database and recreating the app."""

import sqlite3
import threading
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core import clock
from app.db import Database, set_database, to_utc_iso
from app.main import create_app
from app.store.physiological import PhysiologicalStore

ATHLETES = "/api/v1/athletes"
PHYS = "/api/v1/samples/physiological"
TRACK = "/api/v1/samples/tracking"
DEV = "/api/v1/devices/status"
NOW = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)


def at(seconds: float) -> str:
    return (NOW + timedelta(seconds=seconds)).isoformat()


def seed(client, athlete_payload, physiological_payload, tracking_payload, device_status_payload):
    """Register two athletes, send samples for both plus a device status. Returns expected facts."""
    assert client.post(ATHLETES, json=athlete_payload).status_code == 201
    assert client.post(ATHLETES, json={**athlete_payload, "id": "ath-2", "name": "Sam Two", "jersey_number": 2}).status_code == 201
    for s, hr in ((-30, 100), (-10, 120), (-20, None)):  # out of order, one null
        assert client.post(PHYS, json={**physiological_payload, "timestamp": at(s), "heart_rate_bpm": hr}).status_code == 200
    for s, sp in ((-30, 0.0), (-10, 7.5)):
        assert client.post(TRACK, json={**tracking_payload, "timestamp": at(s), "speed_mps": sp}).status_code == 200
    assert client.post(PHYS, json={**physiological_payload, "athlete_id": "ath-2", "timestamp": at(-5), "heart_rate_bpm": 66}).status_code == 200
    assert client.post(DEV, json={**device_status_payload, "timestamp": at(-3), "battery_percent": 0}).status_code == 200


def verify(client):
    assert [a["id"] for a in client.get(ATHLETES).json()] == ["ath-1", "ath-2"]
    assert client.get(f"{ATHLETES}/ath-2").json()["name"] == "Sam Two"
    hist = client.get(f"{ATHLETES}/ath-1/physiological/history").json()
    assert [h["heart_rate_bpm"] for h in hist] == [100, None, 120]  # sorted by time, null preserved
    assert client.get(f"{ATHLETES}/ath-1/physiological/latest").json()["heart_rate_bpm"] == 120
    thist = client.get(f"{ATHLETES}/ath-1/tracking/history").json()
    assert [h["speed_mps"] for h in thist] == [0.0, 7.5]
    assert client.get(f"{ATHLETES}/ath-1/tracking/latest").json()["speed_mps"] == 7.5
    assert client.get(f"{ATHLETES}/ath-2/physiological/latest").json()["heart_rate_bpm"] == 66
    assert client.get(f"{ATHLETES}/ath-2/tracking/latest").status_code == 404
    dev = client.get("/api/v1/devices/dev-1/status/latest").json()
    assert dev["battery_percent"] == 0 and dev["connected"] is True  # real zero preserved


@pytest.fixture
def frozen_clock(monkeypatch):
    monkeypatch.setattr(clock, "utc_now", lambda: NOW)


# ---- schema and file creation ------------------------------------------------

def test_database_and_tables_created_automatically(tmp_path):
    path = tmp_path / "nested" / "deeper" / "engine.db"
    db = Database(path)
    try:
        assert path.exists()
        with db.read() as c:
            names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"athletes", "device_status", "physiological_samples", "tracking_samples"} <= names
    finally:
        db.close()


def test_lifespan_opens_database_at_startup(tmp_path, monkeypatch):
    db = Database(tmp_path / "startup.db")
    set_database(db)
    with TestClient(create_app()) as c:  # context manager runs lifespan
        assert c.get("/api/v1/health").status_code == 200
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM athletes").fetchone()[0] == 0


# ---- survival --------------------------------------------------------------------

def test_data_survives_closing_and_reopening_connection(tmp_path, client, athlete_payload, physiological_payload, tracking_payload, device_status_payload, frozen_clock):
    path = tmp_path / "survive.db"
    db1 = Database(path); set_database(db1)
    seed(client, athlete_payload, physiological_payload, tracking_payload, device_status_payload)
    verify(client)
    db1.close()
    db2 = Database(path); set_database(db2)
    try:
        verify(client)
    finally:
        db2.close()


def test_data_survives_recreating_the_application(tmp_path, athlete_payload, physiological_payload, tracking_payload, device_status_payload, frozen_clock):
    path = tmp_path / "app-restart.db"
    db1 = Database(path); set_database(db1)
    with TestClient(create_app()) as first:
        seed(first, athlete_payload, physiological_payload, tracking_payload, device_status_payload)
    db1.close()
    db2 = Database(path); set_database(db2)  # a brand-new app and connection against the same file
    try:
        with TestClient(create_app()) as second:
            verify(second)
            # and it keeps working: a newer sample becomes latest, an older one does not
            assert second.post(PHYS, json={**physiological_payload, "timestamp": at(-1), "heart_rate_bpm": 130}).status_code == 200
            assert second.post(PHYS, json={**physiological_payload, "timestamp": at(-40), "heart_rate_bpm": 90}).status_code == 200
            assert second.get(f"{ATHLETES}/ath-1/physiological/latest").json()["heart_rate_bpm"] == 130
            assert [h["heart_rate_bpm"] for h in second.get(f"{ATHLETES}/ath-1/physiological/history").json()] == [90, 100, None, 120, 130]
    finally:
        db2.close()


def test_ordering_duplicates_and_cap_persist(tmp_path):
    path = tmp_path / "cap.db"
    db1 = Database(path)
    store = PhysiologicalStore(max_samples=3, db=db1)
    from app.models import PhysiologicalSample
    def s(sec, hr): return PhysiologicalSample(athlete_id="a", timestamp=NOW + timedelta(seconds=sec), source="simulated", device_id="d", heart_rate_bpm=hr)
    for sec, hr in ((10, 21), (12, 23), (11, 22), (12, 33), (5, 20), (13, 24)):  # out of order, duplicate 12 replaced, 5 then trimmed
        store.add(s(sec, hr))
    db1.close()
    db2 = Database(path)
    try:
        reopened = PhysiologicalStore(max_samples=3, db=db2)
        assert [(x.timestamp.second, x.heart_rate_bpm) for x in reopened.recent("a", 10)] == [(11, 22), (12, 33), (13, 24)]
        assert reopened.count("a") == 3
        assert reopened.get("a").heart_rate_bpm == 24
    finally:
        db2.close()


# ---- rejected updates leave persisted data unchanged ------------------------------

def test_rejected_updates_leave_persisted_data_unchanged(tmp_path, client, athlete_payload, physiological_payload, device_status_payload, frozen_clock):
    path = tmp_path / "reject.db"
    db1 = Database(path); set_database(db1)
    assert client.post(ATHLETES, json=athlete_payload).status_code == 201
    assert client.post(PHYS, json={**physiological_payload, "timestamp": at(-2), "heart_rate_bpm": 100}).status_code == 200
    assert client.post(DEV, json={**device_status_payload, "timestamp": at(-2), "battery_percent": 50}).status_code == 200
    # rejected: duplicate athlete, future sample, invalid sample, future device status
    assert client.post(ATHLETES, json={**athlete_payload, "name": "Imposter"}).status_code == 409
    assert client.post(PHYS, json={**physiological_payload, "timestamp": at(600), "heart_rate_bpm": 199}).status_code == 422
    assert client.post(PHYS, json={**physiological_payload, "timestamp": at(-1), "spo2_percent": 500}).status_code == 422
    assert client.post(DEV, json={**device_status_payload, "timestamp": at(600), "battery_percent": 1}).status_code == 422
    db1.close()
    db2 = Database(path); set_database(db2)
    try:
        assert client.get(f"{ATHLETES}/ath-1").json()["name"] == athlete_payload["name"]
        hist = client.get(f"{ATHLETES}/ath-1/physiological/history").json()
        assert [h["heart_rate_bpm"] for h in hist] == [100]
        assert client.get("/api/v1/devices/dev-1/status/latest").json()["battery_percent"] == 50
        with db2.read() as c:
            assert c.execute("SELECT COUNT(*) FROM physiological_samples").fetchone()[0] == 1
    finally:
        db2.close()


# ---- UTC normalisation ---------------------------------------------------------------

def test_to_utc_iso_normalises_offsets():
    a = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
    b = datetime.fromisoformat("2026-10-25T14:00:00+02:00")
    c = datetime.fromisoformat("2026-10-25T07:00:00-05:00")
    assert to_utc_iso(a) == to_utc_iso(b) == to_utc_iso(c) == "2026-10-25T12:00:00.000000+00:00"
    assert to_utc_iso(a) < to_utc_iso(a + timedelta(microseconds=1))  # lexical order == time order


def test_equivalent_offset_timestamps_behave_consistently(client, physiological_payload, tracking_payload, device_status_payload, frozen_clock):
    # Same instant written three ways: one history row, last arrival wins, offset preserved in payload
    utc = "2026-10-25T11:59:00Z"; plus2 = "2026-10-25T13:59:00+02:00"; minus5 = "2026-10-25T06:59:00-05:00"
    for ts, hr in ((utc, 100), (plus2, 101), (minus5, 102)):
        assert client.post(PHYS, json={**physiological_payload, "timestamp": ts, "heart_rate_bpm": hr}).status_code == 200
    hist = client.get(f"{ATHLETES}/ath-1/physiological/history").json()
    assert len(hist) == 1 and hist[0]["heart_rate_bpm"] == 102
    assert datetime.fromisoformat(hist[0]["timestamp"]) == datetime.fromisoformat(utc)
    # an "earlier-looking" wall clock in a later zone is actually older and does not become latest
    assert client.post(PHYS, json={**physiological_payload, "timestamp": "2026-10-25T13:00:00+02:00", "heart_rate_bpm": 50}).status_code == 200  # = 11:00Z
    assert client.get(f"{ATHLETES}/ath-1/physiological/latest").json()["heart_rate_bpm"] == 102
    assert [h["heart_rate_bpm"] for h in client.get(f"{ATHLETES}/ath-1/physiological/history").json()] == [50, 102]
    # tracking and device status use the same rule
    assert client.post(TRACK, json={**tracking_payload, "timestamp": utc, "speed_mps": 1.0}).status_code == 200
    assert client.post(TRACK, json={**tracking_payload, "timestamp": plus2, "speed_mps": 2.0}).status_code == 200
    assert [t["speed_mps"] for t in client.get(f"{ATHLETES}/ath-1/tracking/history").json()] == [2.0]
    assert client.post(DEV, json={**device_status_payload, "timestamp": utc, "battery_percent": 10}).status_code == 200
    assert client.post(DEV, json={**device_status_payload, "timestamp": plus2, "battery_percent": 20}).status_code == 200
    assert client.post(DEV, json={**device_status_payload, "timestamp": "2026-10-25T13:00:00+02:00", "battery_percent": 30}).status_code == 200  # older
    assert client.get("/api/v1/devices/dev-1/status/latest").json()["battery_percent"] == 20
    # window selection compares in UTC too
    w = client.get(f"{ATHLETES}/ath-1/window?seconds=120").json()
    assert [p["heart_rate_bpm"] for p in w["physiological_samples"]] == [102]  # 11:00Z is outside [11:58, 12:00]


# ---- transactions and concurrency -------------------------------------------------

def test_failed_write_rolls_back(tmp_path):
    db = Database(tmp_path / "tx.db")
    try:
        with pytest.raises(RuntimeError):
            with db.transaction() as c:
                c.execute("INSERT INTO athletes VALUES ('x', 'X', 1, 's', 't')")
                raise RuntimeError("boom")
        with db.read() as c:
            assert c.execute("SELECT COUNT(*) FROM athletes").fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError):
            with db.transaction() as c:
                c.execute("INSERT INTO athletes VALUES ('y', 'Y', 1, 's', 't')")
                c.execute("INSERT INTO athletes VALUES ('y', 'Y', 1, 's', 't')")  # duplicate inside the same tx
        with db.read() as c:
            assert c.execute("SELECT COUNT(*) FROM athletes").fetchone()[0] == 0  # whole tx rolled back
    finally:
        db.close()


def test_concurrent_ingest_for_two_athletes(client, physiological_payload, tracking_payload, frozen_clock):
    errors: list = []
    def feed(athlete_id: str, speed: float, n: int):
        try:
            for i in range(n):
                ts = at(-200 + i)
                r1 = client.post(PHYS, json={**physiological_payload, "athlete_id": athlete_id, "timestamp": ts, "heart_rate_bpm": 60 + i})
                r2 = client.post(TRACK, json={**tracking_payload, "athlete_id": athlete_id, "timestamp": ts, "speed_mps": speed})
                if r1.status_code != 200 or r2.status_code != 200:
                    errors.append((athlete_id, i, r1.status_code, r2.status_code, r1.text[:80], r2.text[:80]))
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append((athlete_id, repr(exc)))
    threads = [threading.Thread(target=feed, args=("ath-1", 1.0, 40)), threading.Thread(target=feed, args=("ath-2", 2.0, 40))]
    for t in threads: t.start()
    for t in threads: t.join()
    assert errors == []
    for athlete_id, speed in (("ath-1", 1.0), ("ath-2", 2.0)):
        ph = client.get(f"{ATHLETES}/{athlete_id}/physiological/history?limit=300").json()
        tr = client.get(f"{ATHLETES}/{athlete_id}/tracking/history?limit=300").json()
        assert len(ph) == 40 and len(tr) == 40
        assert {p["athlete_id"] for p in ph} == {athlete_id} and {t["athlete_id"] for t in tr} == {athlete_id}
        assert {t["speed_mps"] for t in tr} == {speed}
        assert [p["heart_rate_bpm"] for p in ph] == list(range(60, 100))  # ordered, nothing lost
