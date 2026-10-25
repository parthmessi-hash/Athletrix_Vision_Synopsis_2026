"""Time and distance at or above a speed threshold: hand-calculated cases, bounds, settings and endpoint."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.core import clock
from app.core.config import Settings
from app.core.distance import Interval, eligible_intervals, estimate_distance, estimate_high_speed, high_speed_portion
from app.core.summary import summarize_window
from app.models import AthleteWindow, TrackingSample

NOW = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
TRACK = "/api/v1/samples/tracking"


def at(seconds: float) -> datetime:
    return NOW + timedelta(seconds=seconds)


def t(seconds, speed, conf=1.0, athlete_id="ath-1"):
    return TrackingSample(athlete_id=athlete_id, timestamp=at(seconds), x_position=0, y_position=0,
                          speed_mps=speed, acceleration_mps2=0, tracking_confidence=conf)


# ---- one interval, hand-calculated ----------------------------------------

def test_fully_below_threshold():
    assert high_speed_portion(Interval(1.0, 3.0, 2.0), 4.0) == (0.0, 0.0)


def test_fully_above_threshold_counts_whole_interval():
    # (5+7)/2 x 2 = 12 m over 2 s
    assert high_speed_portion(Interval(5.0, 7.0, 2.0), 4.0) == (2.0, 12.0)


def test_constantly_at_threshold_qualifies_in_full():
    assert high_speed_portion(Interval(4.0, 4.0, 3.0), 4.0) == (3.0, 12.0)


def test_rising_crossing_worked_example():
    # 2 -> 6 m/s over 2 s, threshold 4: crosses after 1 s; duration 1 s; distance (4+6)/2 x 1 = 5 m
    dur, dist = high_speed_portion(Interval(2.0, 6.0, 2.0), 4.0)
    assert dur == pytest.approx(1.0) and dist == pytest.approx(5.0)


def test_falling_crossing():
    # 6 -> 2 m/s over 2 s, threshold 4: above for the first 1 s; distance (6+4)/2 x 1 = 5 m
    dur, dist = high_speed_portion(Interval(6.0, 2.0, 2.0), 4.0)
    assert dur == pytest.approx(1.0) and dist == pytest.approx(5.0)


def test_uneven_crossing_position():
    # 2 -> 8 m/s over 3 s, threshold 5: crosses at 3 x (5-2)/6 = 1.5 s; duration 1.5 s; distance (5+8)/2 x 1.5 = 9.75 m
    dur, dist = high_speed_portion(Interval(2.0, 8.0, 3.0), 5.0)
    assert dur == pytest.approx(1.5) and dist == pytest.approx(9.75)


def test_threshold_reached_only_at_an_endpoint_counts_zero():
    assert high_speed_portion(Interval(2.0, 4.0, 2.0), 4.0) == pytest.approx((0.0, 0.0))  # rises to it at the end
    assert high_speed_portion(Interval(4.0, 2.0, 2.0), 4.0) == pytest.approx((0.0, 0.0))  # starts on it and falls


def test_stationary_interval_counts_zero_but_is_eligible():
    hs = estimate_high_speed([t(-2, 0.0), t(-1, 0.0), t(0, 0.0)], threshold_mps=5.0)
    assert hs.high_speed_duration_seconds == 0.0 and hs.high_speed_distance_m == 0.0
    assert hs.high_speed_duration_seconds is not None


# ---- windows -----------------------------------------------------------------------

def test_empty_and_single_sample_windows_are_null():
    for samples in ([], [t(0, 9.0)]):
        hs = estimate_high_speed(samples)
        assert hs.high_speed_duration_seconds is None and hs.high_speed_distance_m is None
        assert hs.high_speed_threshold_mps == 5.0


def test_eligible_intervals_none_qualify_gives_real_zeros():
    hs = estimate_high_speed([t(-3, 1.0), t(-2, 2.0), t(-1, 3.0), t(0, 4.0)], threshold_mps=5.0)
    assert hs.high_speed_duration_seconds == 0.0 and hs.high_speed_distance_m == 0.0


def test_uneven_spacing_and_mixed_intervals_sum():
    # threshold 4: [-4,-2] 2->6 over 2 s -> 1 s, 5 m ; [-2,-1.5] 6->6 over 0.5 s -> 0.5 s, 3 m ; [-1.5,0] 6->2 over 1.5 s -> 0.75 s, (6+4)/2 x 0.75 = 3.75 m
    hs = estimate_high_speed([t(-4, 2.0), t(-2, 6.0), t(-1.5, 6.0), t(0, 2.0)], threshold_mps=4.0)
    assert hs.high_speed_duration_seconds == pytest.approx(2.25)
    assert hs.high_speed_distance_m == pytest.approx(11.75)


def test_excluded_gap_and_low_confidence_use_same_eligibility():
    # a 4 s gap (> 3 s) and a low-confidence endpoint are excluded from both estimates identically
    samples = [t(-10, 6.0), t(-6, 6.0), t(-5, 6.0), t(-4, 6.0, conf=0.2), t(-3, 6.0), t(-2, 6.0)]
    intervals, excluded = eligible_intervals(samples)
    assert len(intervals) == 2 and excluded == 3  # [-6,-5] and [-3,-2] eligible; gap, and both sides of the bad sample excluded
    hs = estimate_high_speed(samples, threshold_mps=5.0)
    dist = estimate_distance(samples, 60)
    assert hs.high_speed_duration_seconds == pytest.approx(2.0) and hs.high_speed_distance_m == pytest.approx(12.0)
    assert dist.included_duration_seconds == pytest.approx(2.0) and dist.estimated_distance_m == pytest.approx(12.0)


def test_no_bridging_around_rejected_sample():
    # 6 m/s at -4, bad sample at -3, 6 m/s at -2: bridging would give 2 s / 12 m above threshold; must be null (nothing eligible)
    hs = estimate_high_speed([t(-4, 6.0), t(-3, 6.0, conf=0.1), t(-2, 6.0)], threshold_mps=5.0)
    assert hs.high_speed_duration_seconds is None and hs.high_speed_distance_m is None


# ---- bounds and configuration ---------------------------------------------------

@pytest.mark.parametrize("speeds", [
    [0, 9, 0, 9, 0], [5, 5, 5], [2, 8, 3, 7, 1], [10, 0], [4.9, 5.0, 5.1, 4.9],
])
def test_bounds_hold(speeds):
    samples = [t(-len(speeds) + i, float(v)) for i, v in enumerate(speeds)]
    dist = estimate_distance(samples, 60)
    hs = estimate_high_speed(samples, threshold_mps=5.0)
    assert hs.high_speed_duration_seconds <= dist.included_duration_seconds + 1e-9
    assert hs.high_speed_distance_m <= dist.estimated_distance_m + 1e-9


def test_custom_threshold_changes_result_and_is_echoed():
    samples = [t(-2, 2.0), t(0, 6.0)]
    low = estimate_high_speed(samples, threshold_mps=1.0)
    high = estimate_high_speed(samples, threshold_mps=7.0)
    assert low.high_speed_duration_seconds == pytest.approx(2.0) and low.high_speed_distance_m == pytest.approx(8.0)
    assert high.high_speed_duration_seconds == 0.0 and high.high_speed_distance_m == 0.0
    assert (low.high_speed_threshold_mps, high.high_speed_threshold_mps) == (1.0, 7.0)


@pytest.mark.parametrize("bad", [0, -1.0, float("inf"), float("nan")])
def test_invalid_threshold_rejected(bad):
    with pytest.raises(ValueError):
        estimate_high_speed([t(-1, 1.0), t(0, 1.0)], threshold_mps=bad)


def test_settings_validate_threshold(monkeypatch):
    monkeypatch.setattr(Settings, "model_config", {**Settings.model_config, "env_file": None})
    assert Settings().high_speed_threshold_mps == 5.0
    for bad in (0, -2, float("inf"), float("nan")):
        with pytest.raises(ValidationError):
            Settings(high_speed_threshold_mps=bad)


def test_existing_distance_results_unchanged():
    samples = [t(-4, 0.0), t(-2, 4.0), t(-1.5, 6.0), t(0, 0.0)]
    d = estimate_distance(samples, 60)
    assert d.estimated_distance_m == pytest.approx(11.0) and d.included_duration_seconds == pytest.approx(4.0)
    assert d.included_window_fraction == pytest.approx(4 / 60) and d.excluded_interval_count == 0


# ---- through the summary and endpoint ---------------------------------------------

def test_summary_includes_high_speed_and_keeps_other_metrics():
    track = [t(-3, 2.0), t(0, 8.0)]  # one 3 s interval, exactly at the gap limit
    w = AthleteWindow(athlete_id="ath-1", window_start=at(-60), window_end=at(0), physiological_samples=[], tracking_samples=track)
    s = summarize_window(w)
    assert s.tracking.estimated_distance_m == pytest.approx(15.0) and s.tracking.included_duration_seconds == 3.0
    assert s.tracking.high_speed_duration_seconds == pytest.approx(1.5)
    assert s.tracking.high_speed_distance_m == pytest.approx(9.75)
    assert s.tracking.distance_rules.high_speed_threshold_mps == 5.0
    assert s.tracking.speed_peak_mps == 8.0 and s.tracking.sample_count == 2
    s2 = summarize_window(w, high_speed_threshold_mps=2.0)
    assert s2.tracking.high_speed_duration_seconds == pytest.approx(3.0) and s2.tracking.distance_rules.high_speed_threshold_mps == 2.0


@pytest.fixture
def frozen_clock(monkeypatch):
    monkeypatch.setattr(clock, "utc_now", lambda: NOW)


def test_endpoint_reports_high_speed_per_athlete(client, tracking_payload, frozen_clock):
    def post(aid, s, v):
        assert client.post(TRACK, json={**tracking_payload, "athlete_id": aid, "timestamp": at(s).isoformat(), "speed_mps": v}).status_code == 200
    post("ath-1", -3, 2.0); post("ath-1", 0, 8.0)             # 1.5 s, 9.75 m above 5 m/s
    for i in range(6): post("ath-2", -5 + i, 2.0)               # eligible but all below: zeros
    post("ath-3", -1, 9.0)                                       # single sample: null
    a1 = client.get("/api/v1/athletes/ath-1/summary?seconds=60").json()["tracking"]
    a2 = client.get("/api/v1/athletes/ath-2/summary?seconds=60").json()["tracking"]
    a3 = client.get("/api/v1/athletes/ath-3/summary?seconds=60").json()["tracking"]
    assert a1["high_speed_duration_seconds"] == pytest.approx(1.5) and a1["high_speed_distance_m"] == pytest.approx(9.75)
    assert a1["estimated_distance_m"] == pytest.approx(15.0) and a1["distance_rules"]["high_speed_threshold_mps"] == 5.0
    assert a2["high_speed_duration_seconds"] == 0.0 and a2["high_speed_distance_m"] == 0.0 and a2["estimated_distance_m"] == pytest.approx(10.0)
    assert a3["high_speed_duration_seconds"] is None and a3["high_speed_distance_m"] is None
