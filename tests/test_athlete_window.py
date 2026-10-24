"""Time-window endpoint over both histories, with a controlled clock."""

from datetime import UTC, datetime, timedelta

import pytest

from app.api.athletes import MAX_WINDOW_SECONDS
from app.core import clock
from app.store.physiological import physiological_history_store
from app.store.tracking import tracking_history_store
from app.models import PhysiologicalSample, TrackingSample

NOW = datetime(2026, 10, 24, 12, 0, 0, tzinfo=UTC)
PHYS = "/api/v1/samples/physiological"
TRACK = "/api/v1/samples/tracking"


def at(seconds: float) -> datetime:
    return NOW + timedelta(seconds=seconds)


def window_url(athlete_id="ath-1", seconds=None) -> str:
    url = f"/api/v1/athletes/{athlete_id}/window"
    return url if seconds is None else f"{url}?seconds={seconds}"


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    monkeypatch.setattr(clock, "utc_now", lambda: NOW)


def post_phys(client, base, seconds, hr=100.0, athlete_id="ath-1"):
    r = client.post(PHYS, json={**base, "athlete_id": athlete_id, "timestamp": at(seconds).isoformat(), "heart_rate_bpm": hr})
    assert r.status_code == 200, r.text


def post_track(client, base, seconds, speed=1.0, athlete_id="ath-1"):
    r = client.post(TRACK, json={**base, "athlete_id": athlete_id, "timestamp": at(seconds).isoformat(), "speed_mps": speed})
    assert r.status_code == 200, r.text


def ts_list(samples):
    return [datetime.fromisoformat(s["timestamp"]) for s in samples]


# ---- shape and empty results ---------------------------------------------

def test_empty_window_for_unknown_athlete(client):
    r = client.get(window_url("nobody", 60))
    assert r.status_code == 200
    body = r.json()
    assert body["athlete_id"] == "nobody"
    assert body["physiological_samples"] == []
    assert body["tracking_samples"] == []
    assert datetime.fromisoformat(body["window_end"]) == NOW
    assert datetime.fromisoformat(body["window_start"]) == at(-60)


def test_default_seconds_is_60(client):
    body = client.get(window_url()).json()
    assert datetime.fromisoformat(body["window_end"]) - datetime.fromisoformat(body["window_start"]) == timedelta(seconds=60)


@pytest.mark.parametrize("seconds", [0, -1, MAX_WINDOW_SECONDS + 1, "soon"])
def test_seconds_out_of_range_422(client, seconds):
    assert client.get(window_url(seconds=seconds)).status_code == 422


@pytest.mark.parametrize("seconds", [1, MAX_WINDOW_SECONDS])
def test_seconds_bounds_accepted(client, seconds):
    body = client.get(window_url(seconds=seconds)).json()
    assert datetime.fromisoformat(body["window_start"]) == at(-seconds)


# ---- boundaries ----------------------------------------------------------

def test_inclusive_boundaries_and_exclusions(client, physiological_payload):
    # window of 10 s: [NOW-10, NOW]
    for s in (-10.001, -10, -5, 0, 0.001, 3):  # +3 s is accepted by ingestion (skew tolerance) but must be excluded
        post_phys(client, physiological_payload, s)
    body = client.get(window_url(seconds=10)).json()
    assert ts_list(body["physiological_samples"]) == [at(-10), at(-5), at(0)]


def test_sorted_oldest_to_newest_regardless_of_arrival(client, physiological_payload, tracking_payload):
    for s in (-1, -9, -4):
        post_phys(client, physiological_payload, s)
        post_track(client, tracking_payload, s)
    body = client.get(window_url(seconds=30)).json()
    assert ts_list(body["physiological_samples"]) == [at(-9), at(-4), at(-1)]
    assert ts_list(body["tracking_samples"]) == [at(-9), at(-4), at(-1)]


def test_window_narrower_than_history_only_returns_recent(client, physiological_payload):
    for s in range(-120, 1, 10):  # -120 .. 0 every 10 s
        post_phys(client, physiological_payload, s)
    body = client.get(window_url(seconds=25)).json()
    assert ts_list(body["physiological_samples"]) == [at(-20), at(-10), at(0)]


# ---- both streams, same cutoff -------------------------------------------

def test_both_streams_share_the_same_window(client, physiological_payload, tracking_payload):
    post_phys(client, physiological_payload, -30)   # in
    post_phys(client, physiological_payload, -31)   # out
    post_phys(client, physiological_payload, 2)     # accepted by ingestion, out of window
    post_track(client, tracking_payload, -30)       # in
    post_track(client, tracking_payload, -30.5)     # out
    post_track(client, tracking_payload, 4)         # accepted by ingestion, out of window
    body = client.get(window_url(seconds=30)).json()
    assert ts_list(body["physiological_samples"]) == [at(-30)]
    assert ts_list(body["tracking_samples"]) == [at(-30)]
    assert body["window_end"] == body["window_end"]  # single captured instant
    assert datetime.fromisoformat(body["window_end"]) == NOW


def test_one_stream_empty_other_populated(client, tracking_payload):
    post_track(client, tracking_payload, -5)
    body = client.get(window_url(seconds=10)).json()
    assert body["physiological_samples"] == []
    assert len(body["tracking_samples"]) == 1


def test_window_end_captured_once(client, monkeypatch):
    """Even if the clock advanced between stream reads, both use the same captured end."""
    calls = []
    def ticking():
        calls.append(1)
        return NOW + timedelta(seconds=len(calls))  # advances on every call
    monkeypatch.setattr(clock, "utc_now", ticking)
    body = client.get(window_url(seconds=10)).json()
    assert len(calls) == 1
    assert datetime.fromisoformat(body["window_end"]) == at(1)
    assert datetime.fromisoformat(body["window_start"]) == at(1 - 10)


# ---- separation and preservation -----------------------------------------

def test_athletes_separate(client, physiological_payload, tracking_payload):
    post_phys(client, physiological_payload, -1, hr=100, athlete_id="ath-1")
    post_phys(client, physiological_payload, -1, hr=150, athlete_id="ath-2")
    post_track(client, tracking_payload, -1, speed=1.0, athlete_id="ath-2")
    a1 = client.get(window_url("ath-1", 10)).json()
    a2 = client.get(window_url("ath-2", 10)).json()
    assert [s["heart_rate_bpm"] for s in a1["physiological_samples"]] == [100]
    assert a1["tracking_samples"] == []
    assert [s["heart_rate_bpm"] for s in a2["physiological_samples"]] == [150]
    assert [s["speed_mps"] for s in a2["tracking_samples"]] == [1.0]


def test_measurements_preserved_including_null_and_zero(client, tracking_payload):
    minimal = {"athlete_id": "ath-1", "timestamp": at(-1).isoformat(), "source": "manual", "device_id": "coach"}
    assert client.post(PHYS, json=minimal).status_code == 200
    zero = {**tracking_payload, "timestamp": at(-1).isoformat(), "speed_mps": 0, "acceleration_mps2": 0, "tracking_confidence": 0}
    assert client.post(TRACK, json=zero).status_code == 200
    body = client.get(window_url(seconds=10)).json()
    p, t = body["physiological_samples"][0], body["tracking_samples"][0]
    assert p["heart_rate_bpm"] is None and p["posture"] is None and p["source"] == "manual"
    assert t["speed_mps"] == 0 and t["tracking_confidence"] == 0


def test_rejected_samples_absent_from_window(client, physiological_payload):
    assert client.post(PHYS, json={**physiological_payload, "timestamp": at(-1).isoformat(), "spo2_percent": 500}).status_code == 422
    assert client.post(PHYS, json={**physiological_payload, "timestamp": at(600).isoformat()}).status_code == 422
    assert client.get(window_url(seconds=10)).json()["physiological_samples"] == []


# ---- store-level range query ---------------------------------------------

def test_store_between_inclusive():
    for s in (-3, -2, -1, 0, 1):
        physiological_history_store.add(PhysiologicalSample(athlete_id="a", timestamp=at(s), source="manual", device_id="d"))
    got = physiological_history_store.between("a", at(-2), at(0))
    assert [x.timestamp for x in got] == [at(-2), at(-1), at(0)]
    assert physiological_history_store.between("a", at(5), at(9)) == []
    assert tracking_history_store.between("a", at(-9), at(9)) == []
