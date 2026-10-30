"""GET /athletes/{id}/effort-review: descriptive review of a manually selected effort.

Deterministic fictional data with hand-calculated expectations. Boundaries: before [start-10 s, start), during
[start, end], after (end, end+30 s] capped at the captured backend time. Nothing is interpolated, extrapolated or
bridged, null heart rates stay missing, and the response describes retained samples only.
"""

from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import pytest

from app.core import clock
from app.core.effort import (
    AFTER_SECONDS,
    BEFORE_SECONDS,
    EffortRequestError,
    Period,
    follow_up_elapsed,
    parse_effort_timestamp,
    period_bounds,
    select_period,
    summarize_heart_rate_period,
    validate_effort,
)
from app.models import EffortReview, PhysiologicalSample
from app.models.summary import CONTINUITY_RULE

NOW = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)       # frozen backend time
S = datetime(2026, 10, 20, 10, 0, 0, tzinfo=UTC)         # a fictional effort five days earlier
E = S + timedelta(seconds=20)
PHYS = "/api/v1/samples/physiological"
TRACK = "/api/v1/samples/tracking"
TRK = {"x_position": 0.0, "y_position": 0.0, "acceleration_mps2": 0.0}


def at(base: datetime, seconds: float) -> datetime:
    return base + timedelta(seconds=seconds)


def url(athlete_id="ath-1", start=S, end=E):
    s = start if isinstance(start, str) else start.isoformat()
    e = end if isinstance(end, str) else end.isoformat()
    return f"/api/v1/athletes/{athlete_id}/effort-review?effort_start={quote(s)}&effort_end={quote(e)}"


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    monkeypatch.setattr(clock, "utc_now", lambda: NOW)


def post_phys(client, ts, hr, source="simulated", athlete_id="ath-1"):
    r = client.post(PHYS, json={"athlete_id": athlete_id, "timestamp": ts.isoformat(), "source": source, "device_id": "fictional-d", "heart_rate_bpm": hr})
    assert r.status_code == 200, r.text


def post_track(client, ts, speed, conf=0.9, athlete_id="ath-1", **extra):
    r = client.post(TRACK, json={"athlete_id": athlete_id, "timestamp": ts.isoformat(), "speed_mps": speed, "tracking_confidence": conf, **TRK, **extra})
    assert r.status_code == 200, r.text


def seed_heart_rate(client, athlete_id="ath-1"):
    """Samples placed exactly on and just beyond every boundary. Values are fictional."""
    rows = [
        (at(S, -10.5), 90, "simulated"),     # outside: before the before period
        (at(S, -10), 100, "wearable"),       # before (inclusive start)
        (at(S, -5), None, "simulated"),      # before, null heart rate
        (at(S, -0.001), 104, "simulated"),   # before (exclusive end)
        (S, 110, "simulated"),               # during (inclusive start)
        (at(S, 10), 130, "wearable"),        # during
        (E, 150, "simulated"),               # during (inclusive end)
        (at(E, 0.001), 148, "simulated"),    # after (exclusive start)
        (at(E, 15), None, "manual"),         # after, null heart rate
        (at(E, 30), 120, "simulated"),       # after (inclusive end)
        (at(E, 30.001), 118, "simulated"),   # outside: beyond the after period
    ]
    for ts, hr, src in rows:
        post_phys(client, ts, hr, src, athlete_id)


def seed_movement(client, athlete_id="ath-1"):
    rows = [
        (at(S, -1), 9.0, 1.0),  # outside the effort
        (S, 0.0, 0.9),          # inclusive start, stationary
        (at(S, 1), 2.0, 0.9),
        (at(S, 2), 4.0, 0.95),
        (at(S, 3), 6.0, 0.3),   # low confidence: excludes 2->3 and 3->4
        (at(S, 4), 6.0, 0.9),
        (at(S, 5), 5.0, 0.9),
        (at(S, 10), 5.0, 0.9),  # 5 s gap: excluded
        (E, 1.0, 0.9),          # inclusive end; 10 s gap: excluded
        (at(E, 1), 8.0, 1.0),   # outside the effort
    ]
    for ts, speed, conf in rows:
        post_track(client, ts, speed, conf, athlete_id)


# ---- pure functions ----------------------------------------------------------------------------------------------

def test_period_bounds_are_half_open_where_they_meet_and_after_is_capped_at_now():
    before, during, after = period_bounds(S, E, NOW)
    assert (before.start, before.end, before.start_inclusive, before.end_inclusive) == (at(S, -BEFORE_SECONDS), S, True, False)
    assert (during.start, during.end, during.start_inclusive, during.end_inclusive) == (S, E, True, True)
    assert (after.start, after.end, after.start_inclusive, after.end_inclusive) == (E, at(E, AFTER_SECONDS), False, True)
    _, _, capped = period_bounds(at(NOW, -40), at(NOW, -20), NOW)
    assert capped.end == NOW and capped.start == at(NOW, -20)
    assert not before.contains(S) and during.contains(S) and during.contains(E) and not after.contains(E)
    assert before.contains(at(S, -10)) and not before.contains(at(S, -10.000001))
    assert after.contains(at(E, 30)) and not after.contains(at(E, 30.000001))


@pytest.mark.parametrize("end_offset,expected", [(-30, True), (-31, True), (-29.999, False), (-10, False), (0, False)])
def test_follow_up_elapsed_hand_cases(end_offset, expected):
    assert follow_up_elapsed(at(NOW, end_offset), NOW) is expected


@pytest.mark.parametrize("bad", ["", "   ", "1760954400", "1760954400.5", "-5", "2026-10-20T10:00:00", "2026-10-20 10:00:00Z", "yesterday", "2026-13-01T10:00:00Z", "2026-10-20T25:00:00Z"])
def test_parse_effort_timestamp_rejects_epochs_naive_and_invalid_values(bad):
    with pytest.raises(EffortRequestError) as exc:
        parse_effort_timestamp("effort_start", bad)
    assert "effort_start" in str(exc.value)


def test_parse_effort_timestamp_accepts_offsets_and_compares_as_instants():
    assert parse_effort_timestamp("x", "2026-10-20T12:00:00+02:00") == S
    assert parse_effort_timestamp("x", "2026-10-20T05:00:00.250-05:00") == at(S, 0.25)
    with pytest.raises(EffortRequestError):
        parse_effort_timestamp("x", None)


@pytest.mark.parametrize("start,end,ok", [
    (S, at(S, 1), True), (S, at(S, 120), True), (S, at(S, 0.999), False), (S, at(S, 120.001), False),
    (S, S, False), (at(S, 1), S, False), (at(NOW, -5), NOW, True), (at(NOW, -5), at(NOW, 0.001), False),
])
def test_validate_effort_limits(start, end, ok):
    if ok:
        assert validate_effort(start, end, NOW) == pytest.approx((end - start).total_seconds())
    else:
        with pytest.raises(EffortRequestError):
            validate_effort(start, end, NOW)


def test_select_and_summarize_period_pure_functions():
    def p(sec, hr, src="simulated"):
        return PhysiologicalSample(athlete_id="a", timestamp=at(S, sec), source=src, device_id="d", heart_rate_bpm=hr)
    samples = [p(-10, 100), p(-5, None), p(-0.001, 104), p(0, 110), p(-10.5, 90)]
    before = Period(at(S, -10), S, True, False)
    assert [s.heart_rate_bpm for s in select_period(samples, before)] == [100, None, 104]
    hp = summarize_heart_rate_period(samples, before)
    assert (hp.sample_count, hp.heart_rate_count, hp.heart_rate_mean_bpm, hp.heart_rate_peak_bpm) == (3, 2, 102.0, 104)
    assert hp.max_inter_sample_gap_seconds == pytest.approx(5.0) and hp.first_sample_at == at(S, -10) and hp.last_sample_at == at(S, -0.001)
    empty = summarize_heart_rate_period([], before)
    assert (empty.sample_count, empty.heart_rate_count, empty.heart_rate_mean_bpm, empty.heart_rate_peak_bpm, empty.first_sample_at, empty.max_inter_sample_gap_seconds) == (0, 0, None, None, None, None)


# ---- endpoint: request validation ---------------------------------------------------------------------------------

@pytest.mark.parametrize("query,needle", [
    ("", "effort_start is required"),
    ("effort_start=2026-10-20T10:00:00Z", "effort_end is required"),
    ("effort_start=1760954400&effort_end=2026-10-20T10:00:10Z", "numeric epochs are not accepted"),
    ("effort_start=2026-10-20T10:00:00&effort_end=2026-10-20T10:00:10Z", "explicit timezone"),
    ("effort_start=2026-10-20T10:00:00Z&effort_end=2026-10-20T10:00:10", "effort_end '2026-10-20T10:00:10' must be ISO 8601"),
    ("effort_start=2026-10-20T10:00:00Z&effort_end=2026-10-20T10:00:00Z", "must precede"),
    ("effort_start=2026-10-20T10:00:10Z&effort_end=2026-10-20T10:00:00Z", "must precede"),
    ("effort_start=2026-10-20T10:00:00Z&effort_end=2026-10-20T10:00:00.500Z", "between 1 and 120 seconds"),
    ("effort_start=2026-10-20T10:00:00Z&effort_end=2026-10-20T10:02:00.001Z", "between 1 and 120 seconds"),
    ("effort_start=2026-10-25T11:59:50Z&effort_end=2026-10-25T12:00:00.001Z", "cannot end in the future"),
    ("effort_start=2026-10-20T10:00:00Z&effort_end=2026-10-20T12:00:10+02:00", "must be ISO 8601"),  # unencoded '+' arrives as a space
])
def test_invalid_requests_are_422_with_a_reason(client, query, needle):
    r = client.get("/api/v1/athletes/ath-1/effort-review?" + query)
    assert r.status_code == 422, r.text
    assert needle in r.json()["detail"], r.json()["detail"]


def test_duration_limits_and_end_exactly_now_are_accepted(client):
    assert client.get(url(start=S, end=at(S, 1))).status_code == 200
    assert client.get(url(start=S, end=at(S, 120))).status_code == 200
    body = client.get(url(start=at(NOW, -5), end=NOW)).json()
    assert body["captured_at"] == "2026-10-25T12:00:00Z" and body["follow_up_elapsed"] is False
    assert body["heart_rate"]["after"]["period_start"] == body["heart_rate"]["after"]["period_end"] == "2026-10-25T12:00:00Z"


def test_backend_time_is_read_once_per_request(client, monkeypatch):
    calls = []
    monkeypatch.setattr(clock, "utc_now", lambda: calls.append(1) or NOW)
    assert client.get(url()).status_code == 200
    assert len(calls) == 1


# ---- endpoint: hand-calculated review -----------------------------------------------------------------------------------

def test_heart_rate_periods_hand_calculated_with_exact_boundaries_nulls_and_sources(client):
    seed_heart_rate(client)
    body = client.get(url()).json()
    EffortReview.model_validate(body)
    hr = body["heart_rate"]
    before, during, after = hr["before"], hr["during"], hr["after"]
    assert (before["period_start"], before["period_end"], before["start_inclusive"], before["end_inclusive"]) == ("2026-10-20T09:59:50Z", "2026-10-20T10:00:00Z", True, False)
    assert (during["period_start"], during["period_end"], during["start_inclusive"], during["end_inclusive"]) == ("2026-10-20T10:00:00Z", "2026-10-20T10:00:20Z", True, True)
    assert (after["period_start"], after["period_end"], after["start_inclusive"], after["end_inclusive"]) == ("2026-10-20T10:00:20Z", "2026-10-20T10:00:50Z", False, True)
    # before: 100, null, 104 -> 3 samples, 2 measurements, mean 102, peak 104, gaps 5.000 and 4.999
    assert (before["sample_count"], before["heart_rate_count"], before["heart_rate_mean_bpm"], before["heart_rate_peak_bpm"]) == (3, 2, 102.0, 104.0)
    assert before["first_sample_at"] == "2026-10-20T09:59:50Z" and before["last_sample_at"] == "2026-10-20T09:59:59.999000Z"
    assert before["max_inter_sample_gap_seconds"] == pytest.approx(5.0)
    assert before["sources"] == {"reported": {"simulated": 2, "wearable": 1}, "not_reported": 0}
    # during: 110, 130, 150 -> mean 130, peak 150, gap 10
    assert (during["sample_count"], during["heart_rate_count"], during["heart_rate_mean_bpm"], during["heart_rate_peak_bpm"]) == (3, 3, 130.0, 150.0)
    assert during["first_sample_at"] == "2026-10-20T10:00:00Z" and during["last_sample_at"] == "2026-10-20T10:00:20Z" and during["max_inter_sample_gap_seconds"] == 10.0
    assert during["sources"] == {"reported": {"simulated": 2, "wearable": 1}, "not_reported": 0}
    # after: 148, null, 120 -> 3 samples, 2 measurements, mean 134, peak 148, gaps 14.999 and 15.000
    assert (after["sample_count"], after["heart_rate_count"], after["heart_rate_mean_bpm"], after["heart_rate_peak_bpm"]) == (3, 2, 134.0, 148.0)
    assert after["first_sample_at"] == "2026-10-20T10:00:20.001000Z" and after["last_sample_at"] == "2026-10-20T10:00:50Z"
    assert after["max_inter_sample_gap_seconds"] == pytest.approx(15.0)
    assert after["sources"] == {"reported": {"manual": 1, "simulated": 2}, "not_reported": 0}
    assert body["follow_up_elapsed"] is True and body["effort_duration_seconds"] == 20.0 and body["captured_at"] == "2026-10-25T12:00:00Z"
    assert body["rules"] == {"before_seconds": 10.0, "after_seconds": 30.0, "min_effort_seconds": 1.0, "max_effort_seconds": 120.0}
    assert "recovery" not in str(body).lower() and "fatigue" not in str(body).lower()


def test_movement_hand_calculated_with_confidence_and_gap_exclusions(client):
    seed_movement(client)
    m = client.get(url()).json()["movement"]
    assert m["sample_count"] == 8 and m["speed_peak_mps"] == 6.0
    assert m["first_sample_at"] == "2026-10-20T10:00:00Z" and m["last_sample_at"] == "2026-10-20T10:00:20Z"
    assert m["latest_sample_age_seconds"] == 0.0 and m["max_inter_sample_gap_seconds"] == 10.0
    # eligible: 0->1 (1.0 m), 1->2 (3.0 m), 4->5 (5.5 m); excluded: 2->3, 3->4 (confidence 0.3), 5->10, 10->20 (gaps)
    assert m["estimated_distance_m"] == pytest.approx(9.5)
    assert m["included_duration_seconds"] == pytest.approx(3.0) and m["included_window_fraction"] == pytest.approx(3.0 / 20)
    assert m["excluded_interval_count"] == 4
    assert m["high_speed_duration_seconds"] == pytest.approx(1.0) and m["high_speed_distance_m"] == pytest.approx(5.5)  # only 4->5 is at or above 5 m/s
    assert m["distance_rules"] == {"max_gap_seconds": 3.0, "min_confidence": 0.5, "high_speed_threshold_mps": 5.0, "continuity_rule": CONTINUITY_RULE}
    assert m["sources"] == {"reported": {}, "not_reported": 8}


def test_movement_sources_and_stationary_effort(client):
    for sec in (0, 1, 2):
        post_track(client, at(S, sec), 0.0, 1.0, source="camera", device_id="fictional-cam")
    post_track(client, at(S, 3), 0.0, 1.0)
    m = client.get(url(end=at(S, 3))).json()["movement"]
    assert m["sample_count"] == 4 and m["speed_peak_mps"] == 0.0, "stationary is a real reading, not missing"
    assert m["estimated_distance_m"] == 0.0 and m["included_duration_seconds"] == 3.0 and m["included_window_fraction"] == pytest.approx(1.0)
    assert m["high_speed_duration_seconds"] == 0.0 and m["high_speed_distance_m"] == 0.0
    assert m["sources"] == {"reported": {"camera": 3}, "not_reported": 1}


def test_timezone_equivalent_requests_select_the_same_samples(client):
    seed_heart_rate(client)
    seed_movement(client)
    utc = client.get(url()).json()
    offset = client.get(url(start="2026-10-20T12:00:00+02:00", end="2026-10-20T05:00:20-05:00")).json()

    def norm(v):  # compare timestamps as instants: the response echoes bounds in the offsets that were requested
        if isinstance(v, dict): return {k: norm(x) for k, x in v.items()}
        if isinstance(v, list): return [norm(x) for x in v]
        if isinstance(v, str) and v[:4].isdigit() and "T" in v:
            try: return datetime.fromisoformat(v.replace("Z", "+00:00"))
            except ValueError: return v
        return v
    assert norm(offset) == norm(utc)
    assert offset["effort_start"] == "2026-10-20T12:00:00+02:00" and offset["heart_rate"]["after"]["period_end"] == "2026-10-20T05:00:50-05:00", "bounds are echoed in the requested offsets"
    assert utc["heart_rate"]["during"]["sample_count"] == 3 and utc["movement"]["sample_count"] == 8


def test_null_heart_rates_stay_missing_and_empty_periods_report_zero_and_null(client):
    post_phys(client, at(S, 5), None)
    post_phys(client, at(S, 6), None)
    hr = client.get(url()).json()["heart_rate"]
    d = hr["during"]
    assert (d["sample_count"], d["heart_rate_count"], d["heart_rate_mean_bpm"], d["heart_rate_peak_bpm"]) == (2, 0, None, None)
    assert d["max_inter_sample_gap_seconds"] == 1.0 and d["sources"] == {"reported": {"simulated": 2}, "not_reported": 0}
    for p in (hr["before"], hr["after"]):
        assert (p["sample_count"], p["heart_rate_count"], p["heart_rate_mean_bpm"], p["heart_rate_peak_bpm"], p["first_sample_at"], p["last_sample_at"], p["max_inter_sample_gap_seconds"]) == (0, 0, None, None, None, None, None)
        assert p["sources"] == {"reported": {}, "not_reported": 0}


def test_athletes_are_kept_separate(client):
    seed_heart_rate(client, "ath-1")
    seed_movement(client, "ath-1")
    post_phys(client, at(S, 5), 99, athlete_id="ath-2")
    post_track(client, at(S, 5), 1.0, athlete_id="ath-2")
    a2 = client.get(url("ath-2")).json()
    assert a2["athlete_id"] == "ath-2" and a2["heart_rate"]["during"]["sample_count"] == 1 and a2["heart_rate"]["during"]["heart_rate_peak_bpm"] == 99
    assert a2["movement"]["sample_count"] == 1 and a2["movement"]["estimated_distance_m"] is None
    assert a2["heart_rate"]["before"]["sample_count"] == 0
    a3 = client.get(url("ath-3")).json()
    assert a3["heart_rate"]["during"]["sample_count"] == 0 and a3["movement"]["sample_count"] == 0


def test_incomplete_follow_up_is_capped_at_captured_time_and_flagged(client):
    start, end = at(NOW, -40), at(NOW, -10)
    post_phys(client, at(NOW, -5), 100)    # within the capped after period
    post_phys(client, at(NOW, 0), 101)     # exactly at the cap: included (end inclusive)
    post_phys(client, at(NOW, 3), 102)     # accepted by ingestion (within skew) but after the captured time: excluded
    body = client.get(url(start=start, end=end)).json()
    after = body["heart_rate"]["after"]
    assert body["follow_up_elapsed"] is False
    assert after["period_start"] == "2026-10-25T11:59:50Z" and after["period_end"] == "2026-10-25T12:00:00Z"
    assert after["sample_count"] == 2 and after["heart_rate_peak_bpm"] == 101 and after["last_sample_at"] == "2026-10-25T12:00:00Z"
    full = client.get(url(start=at(NOW, -60), end=at(NOW, -30))).json()
    assert full["follow_up_elapsed"] is True and full["heart_rate"]["after"]["period_end"] == "2026-10-25T12:00:00Z"
    assert full["heart_rate"]["after"]["sample_count"] == 2, "elapsed time does not establish completeness: whatever was retained is counted"


def test_review_uses_retained_samples_only(client):
    # 305 samples one second apart ending at the effort end: the oldest five are trimmed, so the before period is partly gone
    end = S
    for i in range(305):
        post_phys(client, at(end, -(304 - i)), 100)
    body = client.get(url(start=at(end, -20), end=end)).json()
    assert body["heart_rate"]["during"]["sample_count"] == 21
    assert body["heart_rate"]["before"]["sample_count"] == 10
    # only the newest 300 rows (-299 s .. 0 s) are retained: -304 .. -300 s are gone
    older = client.get(url(start=at(end, -300), end=at(end, -290))).json()
    assert older["heart_rate"]["during"]["sample_count"] == 10, "during [-300, -290] has lost its -300 s sample to the cap"
    assert older["heart_rate"]["before"]["sample_count"] == 0, "before [-310, -300) is entirely beyond the cap: empty, not an error"
