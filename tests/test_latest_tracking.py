"""Latest-tracking-sample store and its GET endpoint."""

import pytest

from app.models import TrackingSample
from app.store.tracking import LatestTrackingStore

T0 = "2026-10-15T12:00:00Z"
T1 = "2026-10-15T12:00:01Z"


def make_sample(athlete_id="ath-1", timestamp=T0, speed=5.0):
    return TrackingSample(
        athlete_id=athlete_id, timestamp=timestamp, x_position=10.0, y_position=20.0,
        speed_mps=speed, acceleration_mps2=0.0, tracking_confidence=0.9,
    )


# ---- store unit tests ----------------------------------------------------

def test_store_empty_returns_none():
    assert LatestTrackingStore().get("ath-1") is None


def test_store_stores_and_retrieves():
    store = LatestTrackingStore()
    store.upsert(make_sample(speed=5.0))
    assert store.get("ath-1").speed_mps == 5.0


def test_store_keeps_newer_sample():
    store = LatestTrackingStore()
    store.upsert(make_sample(timestamp=T0, speed=5.0))
    store.upsert(make_sample(timestamp=T1, speed=6.0))
    assert store.get("ath-1").speed_mps == 6.0


def test_store_ignores_older_sample():
    store = LatestTrackingStore()
    store.upsert(make_sample(timestamp=T1, speed=6.0))
    returned = store.upsert(make_sample(timestamp=T0, speed=5.0))
    assert store.get("ath-1").speed_mps == 6.0
    assert returned.speed_mps == 6.0


def test_store_equal_timestamp_takes_most_recent_write():
    store = LatestTrackingStore()
    store.upsert(make_sample(timestamp=T0, speed=5.0))
    store.upsert(make_sample(timestamp=T0, speed=5.5))
    assert store.get("ath-1").speed_mps == 5.5


def test_store_is_per_athlete():
    store = LatestTrackingStore()
    store.upsert(make_sample("ath-1", speed=5.0))
    store.upsert(make_sample("ath-2", speed=7.0))
    assert store.get("ath-1").speed_mps == 5.0
    assert store.get("ath-2").speed_mps == 7.0


# ---- endpoint tests ------------------------------------------------------

def test_get_latest_404_when_unknown(client):
    r = client.get("/api/v1/athletes/nobody/tracking/latest")
    assert r.status_code == 404
    assert "nobody" in r.json()["detail"]


def test_post_then_get_latest(client, tracking_payload):
    r = client.post("/api/v1/samples/tracking", json=tracking_payload)
    assert r.status_code == 200
    r = client.get("/api/v1/athletes/ath-1/tracking/latest")
    assert r.status_code == 200
    assert r.json()["speed_mps"] == tracking_payload["speed_mps"]


def test_get_latest_returns_newest_not_last_posted(client, tracking_payload):
    newer = {**tracking_payload, "timestamp": T1, "speed_mps": 8.0}
    older = {**tracking_payload, "timestamp": T0, "speed_mps": 1.0}
    client.post("/api/v1/samples/tracking", json=newer)
    client.post("/api/v1/samples/tracking", json=older)
    r = client.get("/api/v1/athletes/ath-1/tracking/latest")
    assert r.json()["speed_mps"] == 8.0


def test_tracking_and_physiological_stores_are_independent(client, tracking_payload):
    client.post("/api/v1/samples/tracking", json=tracking_payload)
    assert client.get("/api/v1/athletes/ath-1/tracking/latest").status_code == 200
    assert client.get("/api/v1/athletes/ath-1/physiological/latest").status_code == 404


def test_invalid_post_does_not_touch_store(client, tracking_payload):
    r = client.post("/api/v1/samples/tracking", json={**tracking_payload, "speed_mps": -1})
    assert r.status_code == 422
    assert client.get("/api/v1/athletes/ath-1/tracking/latest").status_code == 404
