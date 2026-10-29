"""While a recording is active, accepted samples for its athletes are written to the recording in the same transaction as the rolling store.

Membership is decided at ingestion (accepted while active), not by the sample's timestamp. Payloads are stored as
normalised (offsets, ids, sources, nulls, zeros, confidences), equal instants replace within the recording, rolling
trimming and restarts leave recorded rows alone, unselected athletes never enter, a stopped recording is never written
again, and the rolling stores and live endpoints behave as before. All data is fictional.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core import clock
from app.core.config import get_settings
from app.db import Database, set_database
from app.main import create_app
from app.store import recordings as recordings_module

NOW = datetime(2026, 10, 26, 20, 0, 0, tzinfo=UTC)
REC = "/api/v1/recordings"
PHYS = "/api/v1/samples/physiological"
TRACK = "/api/v1/samples/tracking"
TRK = {"x_position": 0.0, "y_position": 2.5, "speed_mps": 0.0, "acceleration_mps2": -0.5, "tracking_confidence": 0.9}


@pytest.fixture
def frozen(monkeypatch):
    state = {"now": NOW}
    monkeypatch.setattr(clock, "utc_now", lambda: state["now"])
    return state


@pytest.fixture
def caps(monkeypatch):
    def configure(physiological, tracking):
        monkeypatch.setenv("PHYSIOLOGICAL_RETENTION_SAMPLES", str(physiological))
        monkeypatch.setenv("TRACKING_RETENTION_SAMPLES", str(tracking))
        get_settings.cache_clear()
    yield configure
    get_settings.cache_clear()


def register(client, *ids):
    for n, athlete_id in enumerate(ids, start=1):
        assert client.post("/api/v1/athletes", json={"id": athlete_id, "name": f"Fictional {athlete_id}", "jersey_number": n, "sport": "fictional", "team_id": "t"}).status_code == 201


def start(client, ids):
    r = client.post(REC, json={"name": "session", "athlete_ids": list(ids)})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def post(client, path, athlete_id, ts, **extra):
    base = {"athlete_id": athlete_id, "timestamp": ts if isinstance(ts, str) else ts.isoformat()}
    body = {**base, "source": "simulated", "device_id": "dev", "heart_rate_bpm": 100, **extra} if path == PHYS else {**base, **TRK, **extra}
    return client.post(path, json=body)


def recorded(client, rec_id, athlete_id):
    d = client.get(f"{REC}/{rec_id}").json()
    return next(e for e in d["recorded"] if e["athlete"]["id"] == athlete_id)


def rows(db, table, rec_id, athlete_id):
    with db.read() as c:
        return [json.loads(r["payload"]) for r in c.execute(f"SELECT payload FROM {table} WHERE recording_id = ? AND athlete_id = ? ORDER BY ts_utc", (rec_id, athlete_id)).fetchall()]


def test_accepted_samples_for_selected_athletes_are_recorded_as_normalised_and_others_are_not(client, frozen, isolated_db):
    register(client, "a1", "a2", "a3")
    rec = start(client, ("a1", "a2"))
    assert post(client, PHYS, "a1", "2026-10-26T22:00:00+02:00", heart_rate_bpm=None, breathing_rate_bpm=0.0).status_code == 200      # offset kept, null and zero kept
    assert post(client, TRACK, "a1", NOW, speed_mps=0.0, tracking_confidence=0.0, source="camera", device_id="cam-7").status_code == 200
    assert post(client, PHYS, "a3", NOW).status_code == 200                                                                           # not selected
    assert post(client, PHYS, "unregistered", NOW).status_code == 200                                                                  # accepted by ingestion, not recorded
    p = rows(isolated_db, "recording_physiological_samples", rec, "a1")
    assert len(p) == 1 and p[0]["timestamp"] == "2026-10-26T22:00:00+02:00" and p[0]["heart_rate_bpm"] is None and p[0]["breathing_rate_bpm"] == 0.0
    assert p[0] == client.get("/api/v1/athletes/a1/physiological/latest").json(), "the recorded payload is exactly the stored payload"
    t = rows(isolated_db, "recording_tracking_samples", rec, "a1")
    assert t[0]["speed_mps"] == 0.0 and t[0]["tracking_confidence"] == 0.0 and t[0]["source"] == "camera" and t[0]["device_id"] == "cam-7"
    assert rows(isolated_db, "recording_physiological_samples", rec, "a3") == [] and rows(isolated_db, "recording_physiological_samples", rec, "unregistered") == []
    d = recorded(client, rec, "a1")
    assert d["physiological"] == {"sample_count": 1, "earliest_timestamp": "2026-10-26T20:00:00Z", "latest_timestamp": "2026-10-26T20:00:00Z", "receipt_count": 1} and d["tracking"]["sample_count"] == 1
    assert recorded(client, rec, "a2")["physiological"]["sample_count"] == 0


def test_membership_is_decided_by_ingestion_time_not_the_samples_timestamp(client, frozen):
    register(client, "a1")
    rec = start(client, ("a1",))
    assert post(client, PHYS, "a1", NOW - timedelta(days=3)).status_code == 200, "a historical replay during the recording is recorded as received"
    assert post(client, PHYS, "a1", NOW + timedelta(seconds=4)).status_code == 200, "within the skew allowance: accepted and recorded"
    assert post(client, PHYS, "a1", NOW + timedelta(seconds=6)).status_code == 422, "beyond the allowance: rejected exactly as before, recorded nowhere"
    frozen["now"] = NOW + timedelta(minutes=5)
    assert client.post(f"{REC}/{rec}/stop").status_code == 200
    assert post(client, PHYS, "a1", NOW + timedelta(minutes=1)).status_code == 200, "timestamped inside the session window but received after the stop"
    d = recorded(client, rec, "a1")
    assert d["physiological"]["sample_count"] == 2 and d["physiological"]["earliest_timestamp"] == "2026-10-23T20:00:00Z"
    assert len(client.get("/api/v1/athletes/a1/physiological/history").json()) == 3, "the rolling store took the later sample as usual"


def test_equal_instants_replace_within_the_recording(client, frozen, isolated_db):
    register(client, "a1")
    rec = start(client, ("a1",))
    post(client, PHYS, "a1", NOW, heart_rate_bpm=100)
    post(client, PHYS, "a1", "2026-10-26T22:00:00+02:00", heart_rate_bpm=101)
    post(client, PHYS, "a1", NOW, heart_rate_bpm=102)
    p = rows(isolated_db, "recording_physiological_samples", rec, "a1")
    assert len(p) == 1 and p[0]["heart_rate_bpm"] == 102, "a normalised recording, not an audit log of every arrival"
    assert client.get("/api/v1/athletes/a1/physiological/latest").json()["heart_rate_bpm"] == 102


def test_recorded_rows_survive_rolling_trimming_and_the_rolling_caps_still_apply(client, frozen, caps):
    caps(physiological=3, tracking=2)
    register(client, "a1")
    rec = start(client, ("a1",))
    for s in range(8):
        assert post(client, PHYS, "a1", NOW - timedelta(seconds=60 - s), heart_rate_bpm=100 + s).status_code == 200
        assert post(client, TRACK, "a1", NOW - timedelta(seconds=60 - s), speed_mps=float(s)).status_code == 200
    rd = client.get("/api/v1/athletes/a1/retained-data").json()
    assert rd["physiological"]["sample_count"] == 3 and rd["tracking"]["sample_count"] == 2, "rolling caps unchanged"
    d = recorded(client, rec, "a1")
    assert d["physiological"]["sample_count"] == 8 and d["tracking"]["sample_count"] == 8
    assert d["physiological"]["earliest_timestamp"] == "2026-10-26T19:59:00Z" and d["tracking"]["latest_timestamp"] == "2026-10-26T19:59:07Z"
    live = client.get("/api/v1/athletes/a1/live").json()
    assert live["physiological"]["heart_rate_bpm"] == 107 and live["tracking"]["speed_mps"] == 7.0, "live reading unchanged"


def test_a_failed_recording_write_rolls_back_the_rolling_write_too(client, frozen, monkeypatch):
    register(client, "a1")
    start(client, ("a1",))
    assert post(client, PHYS, "a1", NOW - timedelta(seconds=10), heart_rate_bpm=90).status_code == 200

    def boom(*a, **k):
        raise RuntimeError("simulated disk failure in the recording write")
    monkeypatch.setattr(recordings_module.recording_store, "write_sample", boom)
    failing = TestClient(create_app(), raise_server_exceptions=False)
    r = post(failing, PHYS, "a1", NOW, heart_rate_bpm=150)
    assert r.status_code == 500
    assert client.get("/api/v1/athletes/a1/physiological/latest").json()["heart_rate_bpm"] == 90, "the rolling store did not take the sample either"
    assert len(client.get("/api/v1/athletes/a1/physiological/history").json()) == 1
    monkeypatch.undo()
    assert post(client, PHYS, "a1", NOW, heart_rate_bpm=150).status_code == 200
    assert client.get("/api/v1/athletes/a1/physiological/latest").json()["heart_rate_bpm"] == 150


def test_recording_rows_survive_a_restart_while_rolling_history_is_trimmed(tmp_path, frozen, caps):
    caps(physiological=2, tracking=2)
    path = tmp_path / "rec.db"
    db = Database(path); set_database(db)
    with TestClient(create_app()) as c:
        register(c, "a1", "a2")
        rec = start(c, ("a1",))
        for s in range(5):
            assert post(c, PHYS, "a1", NOW - timedelta(seconds=30 - s), heart_rate_bpm=100 + s).status_code == 200
            assert post(c, PHYS, "a2", NOW - timedelta(seconds=30 - s), heart_rate_bpm=50 + s).status_code == 200
        frozen["now"] = NOW + timedelta(minutes=1)
        assert c.post(f"{REC}/{rec}/stop").status_code == 200
        for s in range(3):
            assert post(c, PHYS, "a1", NOW + timedelta(seconds=s), heart_rate_bpm=200 + s).status_code == 200   # after the stop
    db.close(); set_database(None)
    db = Database(path); set_database(db)
    try:
        with TestClient(create_app()) as c:
            assert c.get(f"{REC}/active").json() == {"recording": None}
            d = next(e for e in c.get(f"{REC}/{rec}").json()["recorded"] if e["athlete"]["id"] == "a1")
            assert d["physiological"] == {"sample_count": 5, "earliest_timestamp": "2026-10-26T19:59:30Z", "latest_timestamp": "2026-10-26T19:59:34Z", "receipt_count": 5}, "recorded rows: all five, none after the stop"
            hist = c.get("/api/v1/athletes/a1/physiological/history").json()
            assert [h["heart_rate_bpm"] for h in hist] == [201, 202], "rolling history: only its capped newest two"
            assert all(e["athlete"]["id"] == "a1" for e in c.get(f"{REC}/{rec}").json()["recorded"]), "a2 was never part of it"
    finally:
        db.close(); set_database(None)
