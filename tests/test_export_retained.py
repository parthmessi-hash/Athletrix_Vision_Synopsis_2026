"""GET /athletes/{id}/retained-samples.jsonl: a replayable snapshot of the samples currently retained for one athlete.

Both streams, oldest first (physiological before tracking at equal instants), written exactly as stored. Not an archive:
rows already trimmed by the newest-300 cap are not exported. All data is fictional.
"""

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.core import clock
from app.core.export import export_filename, export_records
from app.db import Database, set_database, to_utc_iso
from app.models import PhysiologicalSample, TrackingSample
from app.store.history import MAX_HISTORY_SAMPLES

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
T0 = datetime(2026, 10, 20, 10, 0, 0, tzinfo=UTC)
PHYS = "/api/v1/samples/physiological"
TRACK = "/api/v1/samples/tracking"
TRK = {"x_position": 1.0, "y_position": 2.0, "speed_mps": 3.0, "acceleration_mps2": -0.5, "tracking_confidence": 0.9}


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    monkeypatch.setattr(clock, "utc_now", lambda: NOW)


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def post(client, path, body):
    r = client.post(path, json=body)
    assert r.status_code == 200, r.text


def export(client, athlete_id="ath-1"):
    r = client.get(f"/api/v1/athletes/{athlete_id}/retained-samples.jsonl")
    assert r.status_code == 200, r.text
    return r, [json.loads(line) for line in r.text.splitlines()]


# ---- pure function ----------------------------------------------------------------------------------------

def test_export_records_orders_by_instant_then_stream_and_dumps_as_stored():
    p = [PhysiologicalSample(athlete_id="a", timestamp="2026-10-20T12:00:01+02:00", source="wearable", device_id="w", heart_rate_bpm=None, posture="walking")]
    t = [TrackingSample(athlete_id="a", timestamp=at(1), **TRK), TrackingSample(athlete_id="a", timestamp=at(0), source="camera", device_id="c", **TRK)]
    recs = export_records(p, t)
    assert [(r["type"], r["sample"]["timestamp"]) for r in recs] == [("tracking", "2026-10-20T10:00:00Z"), ("physiological", "2026-10-20T12:00:01+02:00"), ("tracking", "2026-10-20T10:00:01Z")]
    assert recs[1]["sample"]["heart_rate_bpm"] is None and recs[1]["sample"]["posture"] == "walking" and recs[1]["sample"]["breathing_rate_bpm"] is None
    assert recs[0]["sample"]["source"] == "camera" and recs[2]["sample"]["source"] is None and recs[2]["sample"]["acceleration_mps2"] == -0.5
    assert export_records([], []) == []


def test_export_filename_is_safe_and_stamped():
    assert export_filename("athlete-1", NOW) == "athletrix-retained-samples-athlete-1-20261025T120000Z.jsonl"
    assert export_filename("../weird id/é", NOW) == "athletrix-retained-samples-weird_id-20261025T120000Z.jsonl"
    assert export_filename("///", NOW).startswith("athletrix-retained-samples-athlete-")


# ---- endpoint -------------------------------------------------------------------------------------------

def test_no_retained_samples_is_a_clear_404(client):
    r = client.get("/api/v1/athletes/nobody/retained-samples.jsonl")
    assert r.status_code == 404 and r.json()["detail"] == "No retained samples for athlete 'nobody' in either stream; nothing to export"


def test_export_content_type_filename_counts_and_ordering(client):
    post(client, TRACK, {"athlete_id": "ath-1", "timestamp": at(2).isoformat(), **TRK})
    post(client, PHYS, {"athlete_id": "ath-1", "timestamp": "2026-10-20T12:00:02+02:00", "source": "wearable", "device_id": "w", "heart_rate_bpm": 100})  # same instant as the tracking row
    post(client, PHYS, {"athlete_id": "ath-1", "timestamp": at(0).isoformat(), "source": "wearable", "device_id": "w", "heart_rate_bpm": None})
    post(client, TRACK, {"athlete_id": "ath-1", "timestamp": at(1).isoformat(), "source": "camera", "device_id": "cam", **TRK, "speed_mps": 0.0})
    r, recs = export(client)
    assert r.headers["content-type"].startswith("application/x-ndjson")
    assert r.headers["content-disposition"] == 'attachment; filename="athletrix-retained-samples-ath-1-20261025T120000Z.jsonl"'
    assert (r.headers["x-athletrix-record-count"], r.headers["x-athletrix-physiological-count"], r.headers["x-athletrix-tracking-count"]) == ("4", "2", "2")
    assert [(x["type"], x["sample"]["timestamp"]) for x in recs] == [
        ("physiological", "2026-10-20T10:00:00Z"), ("tracking", "2026-10-20T10:00:01Z"), ("physiological", "2026-10-20T12:00:02+02:00"), ("tracking", "2026-10-20T10:00:02Z")]
    assert recs[0]["sample"]["heart_rate_bpm"] is None, "null kept"
    assert recs[1]["sample"]["speed_mps"] == 0.0 and recs[1]["sample"]["source"] == "camera" and recs[1]["sample"]["device_id"] == "cam"
    assert recs[3]["sample"]["source"] is None and recs[3]["sample"]["device_id"] is None, "a tracking row without a source stays not reported"
    assert all(set(x) == {"type", "sample"} for x in recs)


def test_athletes_are_exported_separately(client):
    post(client, PHYS, {"athlete_id": "ath-1", "timestamp": at(0).isoformat(), "source": "simulated", "device_id": "d", "heart_rate_bpm": 100})
    post(client, PHYS, {"athlete_id": "ath-2", "timestamp": at(0).isoformat(), "source": "simulated", "device_id": "d", "heart_rate_bpm": 90})
    post(client, TRACK, {"athlete_id": "ath-2", "timestamp": at(0).isoformat(), **TRK})
    _, a1 = export(client, "ath-1")
    _, a2 = export(client, "ath-2")
    assert [x["sample"]["athlete_id"] for x in a1] == ["ath-1"] and [x["sample"]["athlete_id"] for x in a2] == ["ath-2", "ath-2"]
    assert client.get("/api/v1/athletes/ath-3/retained-samples.jsonl").status_code == 404


def test_duplicate_replacement_exports_one_record_with_the_latest_payload(client):
    post(client, PHYS, {"athlete_id": "ath-1", "timestamp": at(0).isoformat(), "source": "simulated", "device_id": "d", "heart_rate_bpm": 100})
    post(client, PHYS, {"athlete_id": "ath-1", "timestamp": "2026-10-20T12:00:00+02:00", "source": "simulated", "device_id": "d", "heart_rate_bpm": 101})
    _, recs = export(client)
    assert len(recs) == 1 and recs[0]["sample"]["heart_rate_bpm"] == 101 and recs[0]["sample"]["timestamp"] == "2026-10-20T12:00:00+02:00"


def test_legacy_stored_rows_without_source_fields_export_as_not_reported(client, isolated_db):
    legacy = {"athlete_id": "old", "timestamp": "2026-10-20T10:00:00Z", **TRK}
    with isolated_db.transaction() as c:
        c.execute("INSERT INTO tracking_samples (athlete_id, ts_utc, payload) VALUES (?, ?, ?)", ("old", to_utc_iso(at(0)), json.dumps(legacy)))
    _, recs = export(client, "old")
    assert recs[0]["sample"]["source"] is None and recs[0]["sample"]["device_id"] is None and recs[0]["sample"]["speed_mps"] == 3.0


def test_optional_physiological_fields_nulls_and_zeros_are_exported_as_stored(client):
    post(client, PHYS, {"athlete_id": "ath-1", "timestamp": at(0).isoformat(), "source": "simulated", "device_id": "d", "heart_rate_bpm": None,
                        "breathing_rate_bpm": 0, "hrv_rmssd_ms": 0.0, "activity_level": 0.0, "posture": "unknown", "spo2_percent": None, "signal_quality": 1.0})
    _, recs = export(client)
    s = recs[0]["sample"]
    assert s["heart_rate_bpm"] is None and s["breathing_rate_bpm"] == 0 and s["hrv_rmssd_ms"] == 0.0 and s["activity_level"] == 0.0
    assert s["posture"] == "unknown" and s["spo2_percent"] is None and s["signal_quality"] == 1.0 and s["hrv_sdnn_ms"] is None


def test_export_is_limited_to_retained_rows(client):
    for i in range(MAX_HISTORY_SAMPLES + 5):
        post(client, PHYS, {"athlete_id": "ath-1", "timestamp": at(i).isoformat(), "source": "simulated", "device_id": "d", "heart_rate_bpm": 100})
    r, recs = export(client)
    assert len(recs) == MAX_HISTORY_SAMPLES and r.headers["x-athletrix-record-count"] == str(MAX_HISTORY_SAMPLES)
    assert recs[0]["sample"]["timestamp"] == at(5).isoformat().replace("+00:00", "Z"), "the five oldest rows were trimmed and cannot be exported"


def test_export_contains_no_roster_device_status_or_other_athletes(client, athlete_payload, device_status_payload):
    assert client.post("/api/v1/athletes", json=athlete_payload).status_code == 201
    assert client.post("/api/v1/devices/status", json=device_status_payload).status_code == 200
    post(client, PHYS, {"athlete_id": "ath-1", "timestamp": at(0).isoformat(), "source": "simulated", "device_id": "d", "heart_rate_bpm": 100})
    post(client, PHYS, {"athlete_id": "ath-2", "timestamp": at(0).isoformat(), "source": "simulated", "device_id": "d", "heart_rate_bpm": 100})
    _, recs = export(client, "ath-1")
    assert all(x["type"] in ("physiological", "tracking") and x["sample"]["athlete_id"] == "ath-1" for x in recs) and len(recs) == 1


# ---- round trip into a second isolated database ------------------------------------------------------------

class Borrowed:
    def __init__(self, client):
        self.client = client

    def __enter__(self):
        return self.client

    def __exit__(self, *exc):
        return False


def test_export_replays_into_a_second_database_with_identical_retained_values(client, tmp_path, isolated_db):
    rp = load("replay_samples")
    rows = [
        (PHYS, {"athlete_id": "ath-1", "timestamp": "2026-10-20T12:00:02+02:00", "source": "wearable", "device_id": "w", "heart_rate_bpm": 115, "posture": "walking"}),
        (PHYS, {"athlete_id": "ath-1", "timestamp": at(1).isoformat(), "source": "wearable", "device_id": "w", "heart_rate_bpm": None, "breathing_rate_bpm": 0}),
        (PHYS, {"athlete_id": "ath-1", "timestamp": at(0).isoformat(), "source": "wearable", "device_id": "w", "heart_rate_bpm": 112}),
        (TRACK, {"athlete_id": "ath-1", "timestamp": at(0).isoformat(), **TRK, "speed_mps": 0.0, "source": "camera", "device_id": "cam"}),
        (TRACK, {"athlete_id": "ath-1", "timestamp": "2026-10-20T05:00:01-05:00", **TRK, "speed_mps": 4.0, "tracking_confidence": 0.3}),
        (TRACK, {"athlete_id": "ath-1", "timestamp": at(2).isoformat(), **TRK, "speed_mps": 6.0}),
    ]
    for path, body in rows:
        post(client, path, body)
    r, recs = export(client)
    exported = tmp_path / "export.jsonl"
    exported.write_text(r.text, encoding="utf-8")
    loaded, errors = rp.load_file(exported)
    assert errors == [] and len(loaded) == 6, "the export is valid replay input"
    first_hist = client.get("/api/v1/athletes/ath-1/physiological/history?limit=10").json()
    first_track = client.get("/api/v1/athletes/ath-1/tracking/history?limit=10").json()
    first_summary = client.get("/api/v1/athletes/ath-1/summary?seconds=60&end=2026-10-20T10:00:02Z").json()

    second = Database(tmp_path / "second.db")
    set_database(second)
    try:
        assert client.get("/api/v1/athletes/ath-1/retained-samples.jsonl").status_code == 404, "the second database starts empty"
        assert rp.main([str(exported)], make_client=lambda url: Borrowed(client), clock=lambda: 0.0, sleep=lambda s: None, out=lambda s: None) == 0
        assert client.get("/api/v1/athletes/ath-1/physiological/history?limit=10").json() == first_hist
        assert client.get("/api/v1/athletes/ath-1/tracking/history?limit=10").json() == first_track
        assert client.get("/api/v1/athletes/ath-1/summary?seconds=60&end=2026-10-20T10:00:02Z").json() == first_summary
        _, again = export(client)
        assert again == recs, "exporting the replayed copy gives the same records"
    finally:
        set_database(isolated_db)
        second.close()
