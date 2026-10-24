"""Summary endpoint and summarize_window with small hand-calculated examples."""

from datetime import UTC, datetime, timedelta

import pytest

from app.api.athletes import MAX_WINDOW_SECONDS
from app.core import clock
from app.core.summary import summarize_window
from app.models import AthleteWindow, PhysiologicalSample, TrackingSample

NOW = datetime(2026, 10, 24, 12, 0, 0, tzinfo=UTC)
PHYS = "/api/v1/samples/physiological"
TRACK = "/api/v1/samples/tracking"


def at(seconds: float) -> datetime:
    return NOW + timedelta(seconds=seconds)


def summary_url(athlete_id="ath-1", seconds=None) -> str:
    url = f"/api/v1/athletes/{athlete_id}/summary"
    return url if seconds is None else f"{url}?seconds={seconds}"


def phys(seconds, hr, athlete_id="ath-1"):
    return PhysiologicalSample(athlete_id=athlete_id, timestamp=at(seconds), source="simulated", device_id="sim", heart_rate_bpm=hr)


def track(seconds, speed, athlete_id="ath-1"):
    return TrackingSample(athlete_id=athlete_id, timestamp=at(seconds), x_position=0, y_position=0,
                          speed_mps=speed, acceleration_mps2=0, tracking_confidence=1)


def window(p=(), t=(), athlete_id="ath-1", start=-60, end=0):
    return AthleteWindow(athlete_id=athlete_id, window_start=at(start), window_end=at(end),
                         physiological_samples=list(p), tracking_samples=list(t))


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    monkeypatch.setattr(clock, "utc_now", lambda: NOW)


def post_phys(client, base, seconds, hr, athlete_id="ath-1"):
    r = client.post(PHYS, json={**base, "athlete_id": athlete_id, "timestamp": at(seconds).isoformat(), "heart_rate_bpm": hr})
    assert r.status_code == 200, r.text


def post_track(client, base, seconds, speed, athlete_id="ath-1"):
    r = client.post(TRACK, json={**base, "athlete_id": athlete_id, "timestamp": at(seconds).isoformat(), "speed_mps": speed})
    assert r.status_code == 200, r.text


# ---- pure function: hand-calculated ---------------------------------------

def test_empty_window_gives_null_metrics_and_zero_counts():
    s = summarize_window(window())
    assert s.physiological.model_dump() == {
        "sample_count": 0, "heart_rate_count": 0, "heart_rate_mean_bpm": None, "heart_rate_peak_bpm": None,
        "first_sample_at": None, "last_sample_at": None,
    }
    assert s.tracking.model_dump() == {
        "sample_count": 0, "speed_peak_mps": None, "first_sample_at": None, "last_sample_at": None,
    }
    assert s.window_start == at(-60) and s.window_end == at(0)


def test_heart_rate_mean_and_peak_hand_calculated():
    # 100, 120, 140 -> mean 120, peak 140
    s = summarize_window(window(p=[phys(-30, 100), phys(-20, 120), phys(-10, 140)]))
    assert s.physiological.sample_count == 3
    assert s.physiological.heart_rate_count == 3
    assert s.physiological.heart_rate_mean_bpm == 120
    assert s.physiological.heart_rate_peak_bpm == 140
    assert s.physiological.first_sample_at == at(-30)
    assert s.physiological.last_sample_at == at(-10)


def test_null_heart_rates_counted_as_samples_but_ignored_in_metrics():
    # heart rates: None, 110, None, 130 -> 4 samples, 2 measurements, mean 120, peak 130
    s = summarize_window(window(p=[phys(-40, None), phys(-30, 110), phys(-20, None), phys(-10, 130)]))
    assert s.physiological.sample_count == 4
    assert s.physiological.heart_rate_count == 2
    assert s.physiological.heart_rate_mean_bpm == 120
    assert s.physiological.heart_rate_peak_bpm == 130
    assert s.physiological.first_sample_at == at(-40)  # timestamps come from all samples, not just measured ones
    assert s.physiological.last_sample_at == at(-10)


def test_all_null_heart_rates_gives_counts_but_null_metrics():
    s = summarize_window(window(p=[phys(-20, None), phys(-10, None)]))
    assert s.physiological.sample_count == 2
    assert s.physiological.heart_rate_count == 0
    assert s.physiological.heart_rate_mean_bpm is None
    assert s.physiological.heart_rate_peak_bpm is None
    assert s.physiological.first_sample_at == at(-20)
    assert s.physiological.last_sample_at == at(-10)


def test_mean_is_arithmetic_not_time_weighted():
    # 60 bpm held for one sample, 180 bpm for one sample 50 s later: arithmetic mean is 120 regardless of spacing
    s = summarize_window(window(p=[phys(-59, 60), phys(-9, 180)]))
    assert s.physiological.heart_rate_mean_bpm == 120


def test_single_measurement_mean_equals_peak():
    s = summarize_window(window(p=[phys(-1, 95.5)]))
    assert s.physiological.heart_rate_mean_bpm == 95.5
    assert s.physiological.heart_rate_peak_bpm == 95.5


def test_peak_speed_hand_calculated():
    # 2.5, 7.25, 4.0 -> peak 7.25
    s = summarize_window(window(t=[track(-30, 2.5), track(-20, 7.25), track(-10, 4.0)]))
    assert s.tracking.sample_count == 3
    assert s.tracking.speed_peak_mps == 7.25
    assert s.tracking.first_sample_at == at(-30)
    assert s.tracking.last_sample_at == at(-10)


def test_zero_speed_peak_is_zero_not_null():
    s = summarize_window(window(t=[track(-20, 0.0), track(-10, 0.0)]))
    assert s.tracking.sample_count == 2
    assert s.tracking.speed_peak_mps == 0.0
    assert s.tracking.speed_peak_mps is not None


def test_one_stream_empty_other_populated():
    s = summarize_window(window(t=[track(-5, 3.0)]))
    assert s.physiological.sample_count == 0 and s.physiological.heart_rate_mean_bpm is None
    assert s.tracking.sample_count == 1 and s.tracking.speed_peak_mps == 3.0


# ---- endpoint ---------------------------------------------------------------

def test_endpoint_empty_for_unknown_athlete(client):
    r = client.get(summary_url("nobody", 60))
    assert r.status_code == 200
    body = r.json()
    assert body["athlete_id"] == "nobody"
    assert body["physiological"]["sample_count"] == 0
    assert body["physiological"]["heart_rate_mean_bpm"] is None
    assert body["physiological"]["first_sample_at"] is None
    assert body["tracking"]["sample_count"] == 0
    assert body["tracking"]["speed_peak_mps"] is None
    assert datetime.fromisoformat(body["window_end"]) == NOW
    assert datetime.fromisoformat(body["window_start"]) == at(-60)


def test_endpoint_hand_calculated_through_ingest(client, physiological_payload, tracking_payload):
    post_phys(client, physiological_payload, -30, 100)
    post_phys(client, physiological_payload, -20, None)
    post_phys(client, physiological_payload, -10, 150)
    post_track(client, tracking_payload, -25, 0.0)
    post_track(client, tracking_payload, -15, 6.5)
    body = client.get(summary_url(seconds=60)).json()
    p, t = body["physiological"], body["tracking"]
    assert (p["sample_count"], p["heart_rate_count"], p["heart_rate_mean_bpm"], p["heart_rate_peak_bpm"]) == (3, 2, 125, 150)
    assert datetime.fromisoformat(p["first_sample_at"]) == at(-30)
    assert datetime.fromisoformat(p["last_sample_at"]) == at(-10)
    assert (t["sample_count"], t["speed_peak_mps"]) == (2, 6.5)
    assert datetime.fromisoformat(t["first_sample_at"]) == at(-25)
    assert datetime.fromisoformat(t["last_sample_at"]) == at(-15)


def test_endpoint_window_boundaries_match_window_endpoint(client, physiological_payload, tracking_payload):
    # window of 10 s: [-10, 0]; -10.001 out, -10 in, 0 in, +3 accepted by ingestion but out
    for s, hr in ((-10.001, 200), (-10, 100), (0, 120), (3, 200)):
        post_phys(client, physiological_payload, s, hr)
    for s, sp in ((-10.5, 9.0), (-10, 1.0), (0, 2.0), (4, 9.0)):
        post_track(client, tracking_payload, s, sp)
    body = client.get(summary_url(seconds=10)).json()
    assert body["physiological"]["sample_count"] == 2
    assert body["physiological"]["heart_rate_mean_bpm"] == 110
    assert body["physiological"]["heart_rate_peak_bpm"] == 120
    assert datetime.fromisoformat(body["physiological"]["first_sample_at"]) == at(-10)
    assert datetime.fromisoformat(body["physiological"]["last_sample_at"]) == at(0)
    assert body["tracking"]["sample_count"] == 2
    assert body["tracking"]["speed_peak_mps"] == 2.0
    # same selection as the window endpoint
    w = client.get(f"/api/v1/athletes/ath-1/window?seconds=10").json()
    assert len(w["physiological_samples"]) == 2 and len(w["tracking_samples"]) == 2
    assert w["window_end"] == body["window_end"] and w["window_start"] == body["window_start"]


def test_endpoint_athlete_separation(client, physiological_payload, tracking_payload):
    post_phys(client, physiological_payload, -5, 100, athlete_id="ath-1")
    post_phys(client, physiological_payload, -5, 180, athlete_id="ath-2")
    post_track(client, tracking_payload, -5, 8.0, athlete_id="ath-2")
    a1 = client.get(summary_url("ath-1", 10)).json()
    a2 = client.get(summary_url("ath-2", 10)).json()
    assert a1["physiological"]["heart_rate_peak_bpm"] == 100 and a1["tracking"]["sample_count"] == 0
    assert a2["physiological"]["heart_rate_peak_bpm"] == 180 and a2["tracking"]["speed_peak_mps"] == 8.0


@pytest.mark.parametrize("seconds", [0, -1, MAX_WINDOW_SECONDS + 1, "soon"])
def test_endpoint_seconds_out_of_range_422(client, seconds):
    assert client.get(summary_url(seconds=seconds)).status_code == 422


def test_endpoint_default_seconds_60(client):
    body = client.get(summary_url()).json()
    assert datetime.fromisoformat(body["window_end"]) - datetime.fromisoformat(body["window_start"]) == timedelta(seconds=60)


def test_endpoint_clock_read_once(client, monkeypatch):
    calls = []
    def ticking():
        calls.append(1)
        return NOW + timedelta(seconds=len(calls))
    monkeypatch.setattr(clock, "utc_now", ticking)
    body = client.get(summary_url(seconds=10)).json()
    assert len(calls) == 1
    assert datetime.fromisoformat(body["window_end"]) == at(1)


def test_endpoint_rejects_unknown_fields_in_models():
    """Summary models forbid extras, so the shape stays stable."""
    from pydantic import ValidationError
    from app.models import TrackingSummary
    with pytest.raises(ValidationError):
        TrackingSummary(sample_count=0, bogus=1)
