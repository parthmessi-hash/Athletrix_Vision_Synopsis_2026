"""Gap-aware distance estimate: pure function, settings and endpoint, with hand-calculated cases."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.core import clock
from app.core.config import Settings
from app.core.distance import estimate_distance
from app.core.summary import summarize_window
from app.models import AthleteWindow, PhysiologicalSample, TrackingRules, TrackingSample

RULES = TrackingRules(min_confidence=0.5)

NOW = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
TRACK = "/api/v1/samples/tracking"


def at(seconds: float) -> datetime:
    return NOW + timedelta(seconds=seconds)


def t(seconds, speed, conf=1.0, athlete_id="ath-1"):
    return TrackingSample(athlete_id=athlete_id, timestamp=at(seconds), x_position=0, y_position=0,
                          speed_mps=speed, acceleration_mps2=0, tracking_confidence=conf)


def est(samples, window=60, **kw):
    return estimate_distance(samples, window, **kw)


# ---- hand-calculated: pure function ---------------------------------------

def test_constant_speed():
    # 2 m/s for 10 s in 1 s steps: 10 intervals x (2+2)/2 x 1 = 20 m; 10 s of a 60 s window
    d = est([t(-10 + i, 2.0) for i in range(11)])
    assert d.estimated_distance_m == pytest.approx(20.0)
    assert d.included_duration_seconds == 10.0
    assert d.included_window_fraction == pytest.approx(10 / 60)
    assert d.excluded_interval_count == 0


def test_changing_speed_and_uneven_spacing():
    # (0->4 m/s over 2 s) = 4 m ; (4->6 over 0.5 s) = 2.5 m ; (6->0 over 1.5 s) = 4.5 m  => 11 m over 4 s
    d = est([t(-4, 0.0), t(-2, 4.0), t(-1.5, 6.0), t(0, 0.0)])
    assert d.estimated_distance_m == pytest.approx(11.0)
    assert d.included_duration_seconds == pytest.approx(4.0)
    assert d.included_window_fraction == pytest.approx(4 / 60)
    assert d.excluded_interval_count == 0


def test_stationary_intervals_give_real_zero_not_null():
    d = est([t(-3, 0.0), t(-2, 0.0), t(-1, 0.0)])
    assert d.estimated_distance_m == 0.0 and d.estimated_distance_m is not None
    assert d.included_duration_seconds == 2.0
    assert d.included_window_fraction == pytest.approx(2 / 60)


def test_empty_window():
    d = est([])
    assert d.estimated_distance_m is None
    assert d.included_duration_seconds == 0.0 and d.included_window_fraction == 0.0
    assert d.excluded_interval_count == 0


def test_single_sample():
    d = est([t(-1, 5.0)])
    assert d.estimated_distance_m is None
    assert d.included_duration_seconds == 0.0 and d.included_window_fraction == 0.0
    assert d.excluded_interval_count == 0


def test_gap_exactly_at_limit_is_included():
    # elapsed 3.0 == max gap 3.0 -> included: (1+3)/2 x 3 = 6 m
    d = est([t(-3, 1.0), t(0, 3.0)], max_gap_seconds=3.0)
    assert d.estimated_distance_m == pytest.approx(6.0)
    assert d.included_duration_seconds == 3.0 and d.excluded_interval_count == 0


def test_gap_above_limit_is_excluded():
    d = est([t(-3.001, 1.0), t(0, 3.0)], max_gap_seconds=3.0)
    assert d.estimated_distance_m is None
    assert d.included_duration_seconds == 0.0 and d.excluded_interval_count == 1


def test_confidence_exactly_at_minimum_is_included():
    d = est([t(-1, 2.0, conf=0.5), t(0, 2.0, conf=0.5)], min_confidence=0.5)
    assert d.estimated_distance_m == pytest.approx(2.0)


def test_confidence_below_minimum_on_either_end_excludes():
    low_prev = est([t(-1, 2.0, conf=0.49), t(0, 2.0, conf=1.0)], min_confidence=0.5)
    low_cur = est([t(-1, 2.0, conf=1.0), t(0, 2.0, conf=0.49)], min_confidence=0.5)
    for d in (low_prev, low_cur):
        assert d.estimated_distance_m is None and d.excluded_interval_count == 1


def test_rejected_interval_is_not_bridged():
    # samples at -4 (2 m/s), -3 (low confidence), -2 (2 m/s): both intervals touch the bad sample, so both are excluded.
    # Bridging -4 -> -2 would have given (2+2)/2 x 2 = 4 m; we must give null and count 2 exclusions.
    d = est([t(-4, 2.0), t(-3, 2.0, conf=0.1), t(-2, 2.0)])
    assert d.estimated_distance_m is None
    assert d.included_duration_seconds == 0.0
    assert d.excluded_interval_count == 2


def test_gap_is_not_bridged_and_no_extrapolation():
    # 1 m/s from -50..-40 (10 s = 10 m), a 5 s gap (excluded), 2 m/s from -35..-30 (5 s = 10 m).
    # The gap itself (5 s at an average of 1.5 m/s would be 7.5 m) must not be added; nor anything before -50 or after -30.
    samples = [t(-50 + i, 1.0) for i in range(11)] + [t(-35 + i, 2.0) for i in range(6)]
    d = est(samples)
    assert d.estimated_distance_m == pytest.approx(20.0)
    assert d.included_duration_seconds == 15.0
    assert d.included_window_fraction == pytest.approx(15 / 60)
    assert d.excluded_interval_count == 1


def test_partial_window_fraction_uses_requested_window():
    d = est([t(-30, 1.0), t(-28, 1.0)], window=30)
    assert d.included_duration_seconds == 2.0
    assert d.included_window_fraction == pytest.approx(2 / 30)


def test_duplicate_timestamp_interval_is_excluded():
    # zero elapsed is not a positive interval
    d = est([t(0, 1.0), t(0, 3.0)])
    assert d.estimated_distance_m is None and d.excluded_interval_count == 1


def test_rules_are_echoed_and_validated():
    d = est([t(-1, 1.0), t(0, 1.0)], max_gap_seconds=2.5, min_confidence=0.8)
    assert (d.max_gap_seconds, d.min_confidence) == (2.5, 0.8)
    with pytest.raises(ValueError):
        est([], max_gap_seconds=0)
    with pytest.raises(ValueError):
        est([], min_confidence=1.5)


def test_settings_validate_distance_rules(monkeypatch):
    monkeypatch.setattr(Settings, "model_config", {**Settings.model_config, "env_file": None})
    assert Settings().distance_max_gap_seconds == 3.0 and Settings().distance_min_confidence == 0.5
    with pytest.raises(ValidationError):
        Settings(distance_max_gap_seconds=0)
    with pytest.raises(ValidationError):
        Settings(distance_min_confidence=1.2)


# ---- through summarize_window: existing metrics unchanged -------------------

def test_summary_carries_distance_and_keeps_existing_metrics():
    phys = [PhysiologicalSample(athlete_id="ath-1", timestamp=at(-5), source="simulated", device_id="d", heart_rate_bpm=120)]
    track = [t(-4, 0.0), t(-2, 4.0), t(-1.5, 6.0), t(0, 0.0)]
    w = AthleteWindow(athlete_id="ath-1", window_start=at(-60), window_end=at(0), physiological_samples=phys, tracking_samples=track, tracking_rules=RULES)
    s = summarize_window(w)
    assert s.physiological.heart_rate_mean_bpm == 120 and s.physiological.sample_count == 1
    assert s.tracking.sample_count == 4 and s.tracking.speed_peak_mps == 6.0
    assert s.tracking.latest_sample_age_seconds == 0.0 and s.tracking.max_inter_sample_gap_seconds == 2.0
    assert s.tracking.estimated_distance_m == pytest.approx(11.0)
    assert s.tracking.included_duration_seconds == pytest.approx(4.0)
    assert s.tracking.distance_rules.max_gap_seconds == 3.0 and s.tracking.distance_rules.min_confidence == 0.5


def test_summary_custom_rules_change_eligibility():
    track = [t(-5, 2.0), t(0, 2.0)]  # 5 s apart
    w = AthleteWindow(athlete_id="ath-1", window_start=at(-60), window_end=at(0), physiological_samples=[], tracking_samples=track, tracking_rules=RULES)
    assert summarize_window(w).tracking.estimated_distance_m is None
    s = summarize_window(w, distance_max_gap_seconds=5.0)
    assert s.tracking.estimated_distance_m == pytest.approx(10.0)
    assert s.tracking.distance_rules.max_gap_seconds == 5.0


# ---- endpoint -------------------------------------------------------------------

@pytest.fixture
def frozen_clock(monkeypatch):
    monkeypatch.setattr(clock, "utc_now", lambda: NOW)


def post(client, base, athlete_id, seconds, speed, conf=1.0):
    r = client.post(TRACK, json={**base, "athlete_id": athlete_id, "timestamp": at(seconds).isoformat(), "speed_mps": speed, "tracking_confidence": conf})
    assert r.status_code == 200, r.text


def test_endpoint_distance_and_athlete_separation(client, tracking_payload, frozen_clock):
    for i in range(11):
        post(client, tracking_payload, "ath-1", -10 + i, 2.0)     # 20 m over 10 s
    for i in range(3):
        post(client, tracking_payload, "ath-2", -2 + i, 0.0)      # stationary: 0.0 over 2 s
    post(client, tracking_payload, "ath-3", -1, 9.0)              # single sample: unavailable
    a1 = client.get("/api/v1/athletes/ath-1/summary?seconds=60").json()["tracking"]
    a2 = client.get("/api/v1/athletes/ath-2/summary?seconds=60").json()["tracking"]
    a3 = client.get("/api/v1/athletes/ath-3/summary?seconds=60").json()["tracking"]
    assert a1["estimated_distance_m"] == pytest.approx(20.0) and a1["included_duration_seconds"] == 10.0
    assert a1["included_window_fraction"] == pytest.approx(10 / 60) and a1["excluded_interval_count"] == 0
    assert a1["distance_rules"] == {"max_gap_seconds": 3.0, "min_confidence": 0.5, "high_speed_threshold_mps": 5.0}
    assert a2["estimated_distance_m"] == 0.0 and a2["included_duration_seconds"] == 2.0 and a2["speed_peak_mps"] == 0.0
    assert a3["estimated_distance_m"] is None and a3["included_duration_seconds"] == 0.0 and a3["included_window_fraction"] == 0.0
    # existing fields still present and unchanged in meaning
    assert a1["sample_count"] == 11 and a1["speed_peak_mps"] == 2.0 and a1["max_inter_sample_gap_seconds"] == 1.0


def test_endpoint_uses_requested_window_for_fraction(client, tracking_payload, frozen_clock):
    for i in range(6):
        post(client, tracking_payload, "ath-1", -5 + i, 1.0)      # 5 s eligible
    assert client.get("/api/v1/athletes/ath-1/summary?seconds=60").json()["tracking"]["included_window_fraction"] == pytest.approx(5 / 60)
    assert client.get("/api/v1/athletes/ath-1/summary?seconds=10").json()["tracking"]["included_window_fraction"] == pytest.approx(5 / 10)
