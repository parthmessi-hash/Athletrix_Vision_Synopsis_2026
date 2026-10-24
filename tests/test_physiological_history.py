"""Bounded, ordered physiological history store and its endpoint."""

from datetime import UTC, datetime, timedelta

import pytest

from app.core import clock
from app.models import PhysiologicalSample
from app.store.history import MAX_HISTORY_SAMPLES
from app.store.physiological import PhysiologicalHistoryStore

BASE = datetime(2026, 10, 24, 12, 0, 0, tzinfo=UTC)
POST = "/api/v1/samples/physiological"


def at(seconds: int) -> datetime:
    return BASE + timedelta(seconds=seconds)


def history_url(athlete_id="ath-1", limit=None) -> str:
    url = f"/api/v1/athletes/{athlete_id}/physiological/history"
    return url if limit is None else f"{url}?limit={limit}"


def make(athlete_id="ath-1", seconds=0, hr=100.0):
    return PhysiologicalSample(athlete_id=athlete_id, timestamp=at(seconds), source="simulated",
                               device_id="sim", heart_rate_bpm=hr)


def payload(base: dict, seconds: int, hr=100.0) -> dict:
    return {**base, "timestamp": at(seconds).isoformat(), "heart_rate_bpm": hr}


@pytest.fixture
def frozen_clock(monkeypatch):
    """Pin backend time to 10 minutes after BASE so all offsets used below are in the past."""
    monkeypatch.setattr(clock, "utc_now", lambda: at(600))


# ---- store unit tests ----------------------------------------------------

def test_store_empty():
    store = PhysiologicalHistoryStore()
    assert store.recent("ath-1", 10) == []
    assert store.count("ath-1") == 0


def test_store_sorted_by_timestamp_despite_arrival_order():
    store = PhysiologicalHistoryStore()
    for s in (3, 1, 2, 0):
        store.add(make(seconds=s))
    assert [x.timestamp for x in store.recent("ath-1", 10)] == [at(0), at(1), at(2), at(3)]


def test_store_equal_timestamp_replaced_by_latest_arrival():
    store = PhysiologicalHistoryStore()
    store.add(make(seconds=5, hr=100))
    store.add(make(seconds=5, hr=120))
    got = store.recent("ath-1", 10)
    assert len(got) == 1
    assert got[0].heart_rate_bpm == 120


def test_store_recent_returns_newest_oldest_to_newest():
    store = PhysiologicalHistoryStore()
    for s in range(10):
        store.add(make(seconds=s))
    assert [x.timestamp for x in store.recent("ath-1", 3)] == [at(7), at(8), at(9)]


def test_store_capacity_drops_oldest():
    store = PhysiologicalHistoryStore(max_samples=5)
    for s in range(8):
        store.add(make(seconds=s))
    assert store.count("ath-1") == 5
    assert [x.timestamp for x in store.recent("ath-1", 10)] == [at(3), at(4), at(5), at(6), at(7)]


def test_store_full_then_older_than_oldest_is_not_kept():
    store = PhysiologicalHistoryStore(max_samples=3)
    for s in (10, 11, 12):
        store.add(make(seconds=s))
    store.add(make(seconds=1))  # older than everything kept and over capacity
    assert [x.timestamp for x in store.recent("ath-1", 10)] == [at(10), at(11), at(12)]


def test_store_full_then_out_of_order_but_newer_than_oldest_is_kept():
    store = PhysiologicalHistoryStore(max_samples=3)
    for s in (10, 11, 13):
        store.add(make(seconds=s))
    store.add(make(seconds=12))
    assert [x.timestamp for x in store.recent("ath-1", 10)] == [at(11), at(12), at(13)]


def test_store_default_capacity_is_named_constant():
    store = PhysiologicalHistoryStore()
    for s in range(MAX_HISTORY_SAMPLES + 20):
        store.add(make(seconds=s))
    assert store.count("ath-1") == MAX_HISTORY_SAMPLES
    assert store.recent("ath-1", 1)[0].timestamp == at(MAX_HISTORY_SAMPLES + 19)


def test_store_athletes_separate():
    store = PhysiologicalHistoryStore()
    store.add(make("ath-1", seconds=0, hr=100))
    store.add(make("ath-2", seconds=0, hr=150))
    assert [x.heart_rate_bpm for x in store.recent("ath-1", 10)] == [100]
    assert [x.heart_rate_bpm for x in store.recent("ath-2", 10)] == [150]


# ---- endpoint tests ------------------------------------------------------

def test_history_empty_list_when_none(client):
    r = client.get(history_url("nobody"))
    assert r.status_code == 200
    assert r.json() == []


def test_history_default_limit_and_order(client, physiological_payload, frozen_clock):
    for s in (2, 0, 1):
        assert client.post(POST, json=payload(physiological_payload, s, hr=100 + s)).status_code == 200
    body = client.get(history_url()).json()
    assert [x["heart_rate_bpm"] for x in body] == [100, 101, 102]


def test_history_limit_returns_newest_oldest_to_newest(client, physiological_payload, frozen_clock):
    for s in range(10):
        client.post(POST, json=payload(physiological_payload, s, hr=100 + s))
    body = client.get(history_url(limit=4)).json()
    assert [x["heart_rate_bpm"] for x in body] == [106, 107, 108, 109]


def test_history_duplicate_timestamp_last_arrival_wins(client, physiological_payload, frozen_clock):
    client.post(POST, json=payload(physiological_payload, 5, hr=100))
    client.post(POST, json=payload(physiological_payload, 5, hr=130))
    body = client.get(history_url()).json()
    assert len(body) == 1
    assert body[0]["heart_rate_bpm"] == 130


@pytest.mark.parametrize("limit", [0, -1, MAX_HISTORY_SAMPLES + 1, "many"])
def test_history_limit_out_of_range_422(client, limit):
    assert client.get(history_url(limit=limit)).status_code == 422


@pytest.mark.parametrize("limit", [1, MAX_HISTORY_SAMPLES])
def test_history_limit_bounds_accepted(client, limit):
    assert client.get(history_url(limit=limit)).status_code == 200


def test_history_athletes_separate(client, physiological_payload, frozen_clock):
    client.post(POST, json={**payload(physiological_payload, 0, hr=100), "athlete_id": "ath-1"})
    client.post(POST, json={**payload(physiological_payload, 0, hr=150), "athlete_id": "ath-2"})
    assert [x["heart_rate_bpm"] for x in client.get(history_url("ath-1")).json()] == [100]
    assert [x["heart_rate_bpm"] for x in client.get(history_url("ath-2")).json()] == [150]


def test_history_preserves_null_measurements(client, frozen_clock):
    minimal = {"athlete_id": "ath-1", "timestamp": at(0).isoformat(), "source": "manual", "device_id": "coach"}
    assert client.post(POST, json=minimal).status_code == 200
    body = client.get(history_url()).json()
    assert body[0]["heart_rate_bpm"] is None
    assert body[0]["posture"] is None


def test_history_latest_unaffected_by_out_of_order_history(client, physiological_payload, frozen_clock):
    client.post(POST, json=payload(physiological_payload, 5, hr=105))
    client.post(POST, json=payload(physiological_payload, 2, hr=102))
    assert client.get("/api/v1/athletes/ath-1/physiological/latest").json()["heart_rate_bpm"] == 105
    assert [x["heart_rate_bpm"] for x in client.get(history_url()).json()] == [102, 105]


def test_invalid_sample_not_in_history(client, physiological_payload, frozen_clock):
    assert client.post(POST, json={**payload(physiological_payload, 0), "spo2_percent": 500}).status_code == 422
    assert client.get(history_url()).json() == []


def test_future_sample_not_in_history(client, physiological_payload, frozen_clock):
    assert client.post(POST, json=payload(physiological_payload, 0, hr=100)).status_code == 200
    future = {**physiological_payload, "timestamp": at(600 + 60).isoformat(), "heart_rate_bpm": 199}
    assert client.post(POST, json=future).status_code == 422
    assert [x["heart_rate_bpm"] for x in client.get(history_url()).json()] == [100]


def test_history_cleared_between_tests(client):
    assert client.get(history_url()).json() == []
