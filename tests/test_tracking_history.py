"""Bounded, ordered tracking history store and its endpoint."""

from datetime import UTC, datetime, timedelta

import pytest

from app.core import clock
from app.models import TrackingSample
from app.store.history import MAX_HISTORY_SAMPLES
from app.store.tracking import TrackingHistoryStore

BASE = datetime(2026, 10, 24, 12, 0, 0, tzinfo=UTC)
POST = "/api/v1/samples/tracking"


def at(seconds: int) -> datetime:
    return BASE + timedelta(seconds=seconds)


def history_url(athlete_id="ath-1", limit=None, stream="tracking") -> str:
    url = f"/api/v1/athletes/{athlete_id}/{stream}/history"
    return url if limit is None else f"{url}?limit={limit}"


def make(athlete_id="ath-1", seconds=0, speed=5.0):
    return TrackingSample(athlete_id=athlete_id, timestamp=at(seconds), x_position=0.0, y_position=0.0,
                          speed_mps=speed, acceleration_mps2=0.0, tracking_confidence=1.0)


def payload(base: dict, seconds: int, speed=5.0) -> dict:
    return {**base, "timestamp": at(seconds).isoformat(), "speed_mps": speed}


@pytest.fixture
def frozen_clock(monkeypatch):
    """Pin backend time to 10 minutes after BASE so all offsets used below are in the past."""
    monkeypatch.setattr(clock, "utc_now", lambda: at(600))


# ---- store unit tests ----------------------------------------------------

def test_store_empty():
    store = TrackingHistoryStore()
    assert store.recent("ath-1", 10) == []


def test_store_sorted_by_timestamp_despite_arrival_order():
    store = TrackingHistoryStore()
    for s in (3, 1, 2, 0):
        store.add(make(seconds=s))
    assert [x.timestamp for x in store.recent("ath-1", 10)] == [at(0), at(1), at(2), at(3)]


def test_store_equal_timestamp_replaced_by_latest_arrival():
    store = TrackingHistoryStore()
    store.add(make(seconds=5, speed=1.0))
    store.add(make(seconds=5, speed=2.0))
    got = store.recent("ath-1", 10)
    assert len(got) == 1 and got[0].speed_mps == 2.0


def test_store_capacity_drops_oldest():
    store = TrackingHistoryStore(max_samples=4)
    for s in range(7):
        store.add(make(seconds=s))
    assert [x.timestamp for x in store.recent("ath-1", 10)] == [at(3), at(4), at(5), at(6)]


def test_store_full_out_of_order_within_cap_kept_older_than_cap_dropped():
    store = TrackingHistoryStore(max_samples=3)
    for s in (10, 11, 13):
        store.add(make(seconds=s))
    store.add(make(seconds=12))  # newer than the oldest kept -> kept, 10 drops
    store.add(make(seconds=1))   # older than everything kept -> not kept
    assert [x.timestamp for x in store.recent("ath-1", 10)] == [at(11), at(12), at(13)]


def test_store_default_capacity_is_named_constant():
    store = TrackingHistoryStore()
    for s in range(MAX_HISTORY_SAMPLES + 5):
        store.add(make(seconds=s))
    assert store.count("ath-1") == MAX_HISTORY_SAMPLES


def test_store_athletes_separate():
    store = TrackingHistoryStore()
    store.add(make("ath-1", speed=1.0))
    store.add(make("ath-2", speed=2.0))
    assert [x.speed_mps for x in store.recent("ath-1", 10)] == [1.0]
    assert [x.speed_mps for x in store.recent("ath-2", 10)] == [2.0]


# ---- endpoint tests ------------------------------------------------------

def test_history_empty_list_when_none(client):
    r = client.get(history_url("nobody"))
    assert r.status_code == 200 and r.json() == []


def test_history_order_and_default_limit(client, tracking_payload, frozen_clock):
    for s in (2, 0, 1):
        assert client.post(POST, json=payload(tracking_payload, s, speed=s)).status_code == 200
    assert [x["speed_mps"] for x in client.get(history_url()).json()] == [0, 1, 2]


def test_history_limit_returns_newest_oldest_to_newest(client, tracking_payload, frozen_clock):
    for s in range(10):
        client.post(POST, json=payload(tracking_payload, s, speed=s))
    assert [x["speed_mps"] for x in client.get(history_url(limit=4)).json()] == [6, 7, 8, 9]


def test_history_duplicate_timestamp_last_arrival_wins(client, tracking_payload, frozen_clock):
    client.post(POST, json=payload(tracking_payload, 5, speed=1.0))
    client.post(POST, json=payload(tracking_payload, 5, speed=3.0))
    body = client.get(history_url()).json()
    assert len(body) == 1 and body[0]["speed_mps"] == 3.0


@pytest.mark.parametrize("limit", [0, -1, MAX_HISTORY_SAMPLES + 1, "many"])
def test_history_limit_out_of_range_422(client, limit):
    assert client.get(history_url(limit=limit)).status_code == 422


@pytest.mark.parametrize("limit", [1, MAX_HISTORY_SAMPLES])
def test_history_limit_bounds_accepted(client, limit):
    assert client.get(history_url(limit=limit)).status_code == 200


def test_history_athletes_separate(client, tracking_payload, frozen_clock):
    client.post(POST, json={**payload(tracking_payload, 0, speed=1.0), "athlete_id": "ath-1"})
    client.post(POST, json={**payload(tracking_payload, 0, speed=2.0), "athlete_id": "ath-2"})
    assert [x["speed_mps"] for x in client.get(history_url("ath-1")).json()] == [1.0]
    assert [x["speed_mps"] for x in client.get(history_url("ath-2")).json()] == [2.0]


def test_tracking_and_physiological_histories_separate(client, tracking_payload, physiological_payload, frozen_clock):
    client.post(POST, json=payload(tracking_payload, 0))
    client.post("/api/v1/samples/physiological", json={**physiological_payload, "timestamp": at(0).isoformat()})
    client.post("/api/v1/samples/physiological", json={**physiological_payload, "timestamp": at(1).isoformat()})
    assert len(client.get(history_url(stream="tracking")).json()) == 1
    assert len(client.get(history_url(stream="physiological")).json()) == 2
    assert "speed_mps" in client.get(history_url(stream="tracking")).json()[0]
    assert "speed_mps" not in client.get(history_url(stream="physiological")).json()[0]


def test_zero_values_preserved_in_history(client, frozen_clock):
    zero = {"athlete_id": "ath-1", "timestamp": at(0).isoformat(), "x_position": 0, "y_position": 0,
            "speed_mps": 0, "acceleration_mps2": 0, "tracking_confidence": 0}
    assert client.post(POST, json=zero).status_code == 200
    body = client.get(history_url()).json()
    assert body[0]["speed_mps"] == 0 and body[0]["tracking_confidence"] == 0


def test_latest_unaffected_by_out_of_order_history(client, tracking_payload, frozen_clock):
    client.post(POST, json=payload(tracking_payload, 5, speed=5.0))
    client.post(POST, json=payload(tracking_payload, 2, speed=2.0))
    assert client.get("/api/v1/athletes/ath-1/tracking/latest").json()["speed_mps"] == 5.0
    assert [x["speed_mps"] for x in client.get(history_url()).json()] == [2.0, 5.0]


def test_invalid_sample_not_in_history(client, tracking_payload, frozen_clock):
    assert client.post(POST, json={**payload(tracking_payload, 0), "speed_mps": -1}).status_code == 422
    assert client.get(history_url()).json() == []


def test_future_sample_not_in_history(client, tracking_payload, frozen_clock):
    assert client.post(POST, json=payload(tracking_payload, 0, speed=1.0)).status_code == 200
    future = {**tracking_payload, "timestamp": at(600 + 60).isoformat(), "speed_mps": 9.0}
    assert client.post(POST, json=future).status_code == 422
    assert [x["speed_mps"] for x in client.get(history_url()).json()] == [1.0]


def test_history_cleared_between_tests(client):
    assert client.get(history_url()).json() == []
