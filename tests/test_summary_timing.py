"""latest_sample_age_seconds and max_inter_sample_gap_seconds, hand-calculated."""

from datetime import UTC, datetime, timedelta

import pytest

from app.core import clock
from app.core.summary import latest_sample_age_seconds, max_inter_sample_gap_seconds, summarize_window
from app.models import AthleteWindow, PhysiologicalSample, TrackingSample

NOW = datetime(2026, 10, 24, 12, 0, 0, tzinfo=UTC)
PHYS = "/api/v1/samples/physiological"
TRACK = "/api/v1/samples/tracking"


def at(seconds: float) -> datetime:
    return NOW + timedelta(seconds=seconds)


def phys(seconds, hr=100.0):
    return PhysiologicalSample(athlete_id="ath-1", timestamp=at(seconds), source="simulated", device_id="sim", heart_rate_bpm=hr)


def track(seconds, speed=1.0):
    return TrackingSample(athlete_id="ath-1", timestamp=at(seconds), x_position=0, y_position=0,
                          speed_mps=speed, acceleration_mps2=0, tracking_confidence=1)


def window(p=(), t=(), end=0):
    return AthleteWindow(athlete_id="ath-1", window_start=at(end - 60), window_end=at(end),
                         physiological_samples=list(p), tracking_samples=list(t))


# ---- helper functions ----------------------------------------------------

def test_age_none_when_empty():
    assert latest_sample_age_seconds([], at(0)) is None


def test_age_exact_zero_when_newest_equals_window_end():
    assert latest_sample_age_seconds([at(-10), at(0)], at(0)) == 0.0


def test_age_hand_calculated():
    assert latest_sample_age_seconds([at(-30), at(-7.5)], at(0)) == 7.5


def test_gap_none_when_fewer_than_two():
    assert max_inter_sample_gap_seconds([]) is None
    assert max_inter_sample_gap_seconds([at(-3)]) is None


def test_gap_uneven_spacing_hand_calculated():
    # -50, -49, -30, -29.5, -2  -> gaps 1, 19, 0.5, 27.5 -> max 27.5
    assert max_inter_sample_gap_seconds([at(-50), at(-49), at(-30), at(-29.5), at(-2)]) == 27.5


def test_gap_even_spacing():
    assert max_inter_sample_gap_seconds([at(-3), at(-2), at(-1), at(0)]) == 1.0


def test_gap_two_samples():
    assert max_inter_sample_gap_seconds([at(-12), at(-4)]) == 8.0


# ---- through summarize_window --------------------------------------------

def test_summary_empty_streams_null_timing():
    s = summarize_window(window())
    assert s.physiological.latest_sample_age_seconds is None
    assert s.physiological.max_inter_sample_gap_seconds is None
    assert s.tracking.latest_sample_age_seconds is None
    assert s.tracking.max_inter_sample_gap_seconds is None


def test_summary_single_sample_age_but_no_gap():
    s = summarize_window(window(p=[phys(-4)], t=[track(-9)]))
    assert s.physiological.latest_sample_age_seconds == 4.0
    assert s.physiological.max_inter_sample_gap_seconds is None
    assert s.tracking.latest_sample_age_seconds == 9.0
    assert s.tracking.max_inter_sample_gap_seconds is None


def test_summary_uneven_physiological_spacing():
    # window_end = 0; samples at -40, -39, -10, -2 -> age 2, gaps 1, 29, 8 -> max 29
    s = summarize_window(window(p=[phys(-40), phys(-39), phys(-10), phys(-2)]))
    assert s.physiological.latest_sample_age_seconds == 2.0
    assert s.physiological.max_inter_sample_gap_seconds == 29.0


def test_summary_exact_zero_age():
    s = summarize_window(window(p=[phys(-5), phys(0)], t=[track(0)]))
    assert s.physiological.latest_sample_age_seconds == 0.0
    assert s.tracking.latest_sample_age_seconds == 0.0


def test_summary_streams_independent():
    # physiological: -20, -10 -> age 10, gap 10 ; tracking: -30, -29, -1 -> age 1, gaps 1, 28 -> max 28
    s = summarize_window(window(p=[phys(-20), phys(-10)], t=[track(-30), track(-29), track(-1)]))
    assert (s.physiological.latest_sample_age_seconds, s.physiological.max_inter_sample_gap_seconds) == (10.0, 10.0)
    assert (s.tracking.latest_sample_age_seconds, s.tracking.max_inter_sample_gap_seconds) == (1.0, 28.0)


def test_summary_null_heart_rates_still_count_for_timing():
    # timing uses every sample's timestamp, even when the measurement is null
    s = summarize_window(window(p=[phys(-30, None), phys(-5, None)]))
    assert s.physiological.heart_rate_count == 0
    assert s.physiological.latest_sample_age_seconds == 5.0
    assert s.physiological.max_inter_sample_gap_seconds == 25.0


def test_summary_uses_window_end_not_wall_clock():
    s = summarize_window(window(p=[phys(95)], end=100))
    assert s.physiological.latest_sample_age_seconds == 5.0


def test_existing_metrics_preserved_alongside_timing():
    s = summarize_window(window(p=[phys(-20, 100), phys(-10, 140)], t=[track(-15, 0.0)]))
    assert s.physiological.heart_rate_mean_bpm == 120 and s.physiological.heart_rate_peak_bpm == 140
    assert s.tracking.speed_peak_mps == 0.0
    assert s.tracking.latest_sample_age_seconds == 15.0


# ---- endpoint with a pinned clock ----------------------------------------

@pytest.fixture
def frozen_clock(monkeypatch):
    monkeypatch.setattr(clock, "utc_now", lambda: NOW)


def test_endpoint_timing_fields(client, physiological_payload, tracking_payload, frozen_clock):
    for s in (-45, -44, -3):
        assert client.post(PHYS, json={**physiological_payload, "timestamp": at(s).isoformat()}).status_code == 200
    assert client.post(TRACK, json={**tracking_payload, "timestamp": at(0).isoformat()}).status_code == 200
    body = client.get("/api/v1/athletes/ath-1/summary?seconds=60").json()
    p, t = body["physiological"], body["tracking"]
    assert p["latest_sample_age_seconds"] == 3.0
    assert p["max_inter_sample_gap_seconds"] == 41.0   # gaps 1 and 41
    assert t["latest_sample_age_seconds"] == 0.0        # exact zero preserved, not null
    assert t["max_inter_sample_gap_seconds"] is None    # single sample


def test_endpoint_empty_timing_null(client, frozen_clock):
    body = client.get("/api/v1/athletes/nobody/summary?seconds=30").json()
    for stream in ("physiological", "tracking"):
        assert body[stream]["latest_sample_age_seconds"] is None
        assert body[stream]["max_inter_sample_gap_seconds"] is None
