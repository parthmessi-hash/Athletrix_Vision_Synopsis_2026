"""Latest-physiological-sample store and its GET endpoint."""

import pytest

from app.models import PhysiologicalSample
from app.store.physiological import LatestPhysiologicalStore, latest_physiological_store

T0 = "2026-10-15T12:00:00Z"
T1 = "2026-10-15T12:00:01Z"


@pytest.fixture(autouse=True)
def clean_store():
    latest_physiological_store.clear()
    yield
    latest_physiological_store.clear()


def make_sample(athlete_id="ath-1", timestamp=T0, hr=120):
    return PhysiologicalSample(
        athlete_id=athlete_id, timestamp=timestamp, source="wearable", device_id="dev-1", heart_rate_bpm=hr
    )


# ---- store unit tests ----------------------------------------------------

def test_store_empty_returns_none():
    assert LatestPhysiologicalStore().get("ath-1") is None


def test_store_keeps_newer_sample():
    store = LatestPhysiologicalStore()
    store.upsert(make_sample(timestamp=T0, hr=100))
    store.upsert(make_sample(timestamp=T1, hr=110))
    assert store.get("ath-1").heart_rate_bpm == 110


def test_store_ignores_older_sample():
    store = LatestPhysiologicalStore()
    store.upsert(make_sample(timestamp=T1, hr=110))
    returned = store.upsert(make_sample(timestamp=T0, hr=100))
    assert store.get("ath-1").heart_rate_bpm == 110
    assert returned.heart_rate_bpm == 110


def test_store_equal_timestamp_takes_most_recent_write():
    store = LatestPhysiologicalStore()
    store.upsert(make_sample(timestamp=T0, hr=100))
    store.upsert(make_sample(timestamp=T0, hr=105))
    assert store.get("ath-1").heart_rate_bpm == 105


def test_store_is_per_athlete():
    store = LatestPhysiologicalStore()
    store.upsert(make_sample("ath-1", hr=100))
    store.upsert(make_sample("ath-2", hr=150))
    assert store.get("ath-1").heart_rate_bpm == 100
    assert store.get("ath-2").heart_rate_bpm == 150


def test_store_compares_across_timezones():
    store = LatestPhysiologicalStore()
    store.upsert(make_sample(timestamp="2026-10-15T12:00:00Z", hr=100))
    # 13:00:00 at +02:00 is 11:00:00Z, i.e. older than the stored sample.
    store.upsert(make_sample(timestamp="2026-10-15T13:00:00+02:00", hr=90))
    assert store.get("ath-1").heart_rate_bpm == 100


# ---- endpoint tests ------------------------------------------------------

def test_get_latest_404_when_unknown(client):
    r = client.get("/api/v1/athletes/nobody/physiological/latest")
    assert r.status_code == 404
    assert "nobody" in r.json()["detail"]


def test_post_then_get_latest(client, physiological_payload):
    r = client.post("/api/v1/samples/physiological", json=physiological_payload)
    assert r.status_code == 200
    r = client.get("/api/v1/athletes/ath-1/physiological/latest")
    assert r.status_code == 200
    assert r.json()["heart_rate_bpm"] == physiological_payload["heart_rate_bpm"]


def test_get_latest_returns_newest_not_last_posted(client, physiological_payload):
    newer = {**physiological_payload, "timestamp": T1, "heart_rate_bpm": 160}
    older = {**physiological_payload, "timestamp": T0, "heart_rate_bpm": 100}
    client.post("/api/v1/samples/physiological", json=newer)
    client.post("/api/v1/samples/physiological", json=older)
    r = client.get("/api/v1/athletes/ath-1/physiological/latest")
    assert r.json()["heart_rate_bpm"] == 160


def test_invalid_post_does_not_touch_store(client, physiological_payload):
    r = client.post("/api/v1/samples/physiological", json={**physiological_payload, "spo2_percent": 500})
    assert r.status_code == 422
    r = client.get("/api/v1/athletes/ath-1/physiological/latest")
    assert r.status_code == 404
