"""Availability context on window, summary and effort-review responses: retained-versus-selected per stream.

The metadata is read with the samples it describes, so `selected_count` always equals the number of samples returned
(or each period's sample_count), the retained facts match the retained-data endpoint, and each stream reports its
own cap. Nothing is estimated: no expected count, rate, coverage or reason for absence, and `at_cap` is the engine's
only evidence about removal. Boundary rules, figures and sources are unchanged. All data is fictional.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.core import clock
from app.core.config import get_settings
from app.models import AthleteSummary, AthleteWindow, EffortReview, StreamAvailability

PHYS = "/api/v1/samples/physiological"
TRACK = "/api/v1/samples/tracking"
T0 = datetime(2026, 10, 20, 10, 0, 0, tzinfo=UTC)      # a fictional recording well in the past
NOW = datetime(2026, 10, 20, 11, 0, 0, tzinfo=UTC)
TRK = {"x_position": 1.0, "y_position": 2.0, "speed_mps": 3.0, "acceleration_mps2": 0.0, "tracking_confidence": 0.9}


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def iso(seconds: float) -> str:
    return at(seconds).isoformat().replace("+00:00", "Z")


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    monkeypatch.setattr(clock, "utc_now", lambda: NOW)


@pytest.fixture
def caps(monkeypatch):
    def configure(physiological, tracking):
        monkeypatch.setenv("PHYSIOLOGICAL_RETENTION_SAMPLES", str(physiological))
        monkeypatch.setenv("TRACKING_RETENTION_SAMPLES", str(tracking))
        get_settings.cache_clear()
    yield configure
    get_settings.cache_clear()


def post_phys(client, ts, hr=100, athlete_id="ath-1", source="simulated"):
    body = {"athlete_id": athlete_id, "timestamp": ts.isoformat(), "source": source, "device_id": "sim", "heart_rate_bpm": hr}
    assert client.post(PHYS, json=body).status_code == 200


def post_track(client, ts, athlete_id="ath-1", **extra):
    assert client.post(TRACK, json={"athlete_id": athlete_id, "timestamp": ts.isoformat(), **TRK, **extra}).status_code == 200


def window(client, seconds, end, athlete_id="ath-1"):
    r = client.get(f"/api/v1/athletes/{athlete_id}/window", params={"seconds": seconds, "end": end.isoformat()})
    assert r.status_code == 200, r.text
    return r.json()


def summary(client, seconds, end, athlete_id="ath-1"):
    r = client.get(f"/api/v1/athletes/{athlete_id}/summary", params={"seconds": seconds, "end": end.isoformat()})
    assert r.status_code == 200, r.text
    return r.json()


def effort(client, start, end, athlete_id="ath-1"):
    r = client.get(f"/api/v1/athletes/{athlete_id}/effort-review", params={"effort_start": start.isoformat(), "effort_end": end.isoformat()})
    assert r.status_code == 200, r.text
    return r.json()


def avail(**kw):
    base = {"max_samples": 300, "retained_count": 0, "retained_earliest": None, "retained_latest": None, "at_cap": False, "selected_count": 0, "selected_first": None, "selected_last": None}
    return {**base, **kw}


# ---- window and summary ---------------------------------------------------------------------------------------------

def test_no_retained_samples_at_all_is_distinguished_from_none_in_the_period(client):
    body = window(client, 60, at(60))
    assert body["availability"] == {"physiological": avail(), "tracking": avail()}
    AthleteWindow.model_validate(body)
    post_track(client, at(200))                                 # retained, but outside the window
    body = window(client, 60, at(60))
    assert body["tracking_samples"] == []
    assert body["availability"]["tracking"] == avail(retained_count=1, retained_earliest=iso(200), retained_latest=iso(200)), "retained elsewhere, nothing selected: the window says so without guessing why"
    assert body["availability"]["physiological"] == avail()


def test_partial_ranges_and_internal_gaps_are_described_not_filled(client):
    for s in list(range(20, 31)) + list(range(50, 61)):        # 10:00:20..30 and 10:00:50..60: an internal gap, nothing before 20 s
        post_phys(client, at(s), hr=100 + s)
    body = window(client, 60, at(60))                           # window [10:00:00, 10:01:00]
    a = body["availability"]["physiological"]
    assert a == avail(retained_count=22, retained_earliest=iso(20), retained_latest=iso(60), selected_count=22, selected_first=iso(20), selected_last=iso(60))
    assert a["selected_count"] == len(body["physiological_samples"]) and len(body["physiological_samples"]) == 22
    assert "coverage" not in a and "expected" not in str(a), "no coverage fraction or expected count is invented"
    later = window(client, 20, at(80))                          # [10:00:60, 10:01:20]: retained history ends before the window ends
    assert later["availability"]["physiological"] == avail(retained_count=22, retained_earliest=iso(20), retained_latest=iso(60), selected_count=1, selected_first=iso(60), selected_last=iso(60))
    earlier = window(client, 10, at(15))                        # [10:00:05, 10:00:15]: retained history begins after the window ends
    assert earlier["physiological_samples"] == [] and earlier["availability"]["physiological"]["selected_count"] == 0 and earlier["availability"]["physiological"]["retained_count"] == 22


def test_each_stream_reports_its_own_cap_and_at_cap_only_when_full(client, caps):
    caps(physiological=3, tracking=5)
    for s in range(6):
        post_phys(client, at(s)); post_track(client, at(s))
    body = window(client, 10, at(5))
    p, t = body["availability"]["physiological"], body["availability"]["tracking"]
    assert (p["max_samples"], p["retained_count"], p["at_cap"], p["retained_earliest"]) == (3, 3, True, iso(3))
    assert (t["max_samples"], t["retained_count"], t["at_cap"], t["retained_earliest"]) == (5, 5, True, iso(1))
    assert p["selected_count"] == 3 and t["selected_count"] == 5
    post_track(client, at(6), athlete_id="ath-2")
    other = window(client, 10, at(6), athlete_id="ath-2")["availability"]["tracking"]
    assert other["retained_count"] == 1 and other["at_cap"] is False and other["max_samples"] == 5


def test_summary_availability_agrees_with_its_counts_and_the_retained_data_endpoint(client):
    for s in (0, 5, 10, 70):
        post_phys(client, at(s), hr=None if s == 5 else 100)   # a null heart rate still counts as a selected sample
        post_track(client, at(s))
    body = summary(client, 60, at(60))
    a = body["availability"]
    assert a["physiological"]["selected_count"] == body["physiological"]["sample_count"] == 3 and body["physiological"]["heart_rate_count"] == 2
    assert a["tracking"]["selected_count"] == body["tracking"]["sample_count"] == 3
    assert (a["physiological"]["selected_first"], a["physiological"]["selected_last"]) == (body["physiological"]["first_sample_at"], body["physiological"]["last_sample_at"])
    rd = client.get("/api/v1/athletes/ath-1/retained-data").json()
    for stream in ("physiological", "tracking"):
        assert (a[stream]["retained_count"], a[stream]["retained_earliest"], a[stream]["retained_latest"], a[stream]["max_samples"]) == (rd[stream]["sample_count"], rd[stream]["earliest_timestamp"], rd[stream]["latest_timestamp"], rd[stream]["max_samples"])
    assert body["physiological"]["sources"] == {"reported": {"simulated": 3}, "not_reported": 0}, "sources unchanged"
    AthleteSummary.model_validate(body)


def test_live_window_and_summary_carry_availability_too(client):
    post_phys(client, NOW - timedelta(seconds=5))
    body = client.get("/api/v1/athletes/ath-1/window?seconds=60").json()
    assert body["availability"]["physiological"]["selected_count"] == 1 and body["availability"]["tracking"]["retained_count"] == 0
    assert client.get("/api/v1/athletes/ath-1/summary?seconds=60").json()["availability"]["physiological"]["retained_count"] == 1


def test_window_samples_and_availability_come_from_one_read(client, monkeypatch):
    from app.db import get_database
    db = get_database()
    depth = []
    real_read = db.read

    def counting_read():
        depth.append(1)
        return real_read()
    monkeypatch.setattr(db, "read", counting_read)
    window(client, 60, at(60))
    assert depth and depth[0] == 1, "the endpoint opens the read itself; store reads nest inside it"


# ---- effort review -----------------------------------------------------------------------------------------------------

def test_effort_review_availability_per_period_matches_the_periods_and_changes_no_figure(client):
    start, end = at(100), at(120)
    for s in range(95, 150):                                    # heart rate from 5 s before the effort to 30 s after
        post_phys(client, at(s), hr=100 + s)
    for s in range(100, 121):
        post_track(client, at(s), speed_mps=2.0, tracking_confidence=1.0)
    body = effort(client, start, end)
    EffortReview.model_validate(body)
    a = body["availability"]
    hr = body["heart_rate"]
    for name in ("before", "during", "after"):
        assert a["heart_rate"][name]["selected_count"] == hr[name]["sample_count"], name
        assert (a["heart_rate"][name]["selected_first"], a["heart_rate"][name]["selected_last"]) == (hr[name]["first_sample_at"], hr[name]["last_sample_at"]), name
        assert (a["heart_rate"][name]["retained_count"], a["heart_rate"][name]["retained_earliest"], a["heart_rate"][name]["max_samples"]) == (55, iso(95), 300), "the same retained facts in every period"
    assert hr["before"]["sample_count"] == 5 and hr["during"]["sample_count"] == 21 and hr["after"]["sample_count"] == 29, "period boundaries unchanged: before [90, 100), during [100, 120], after (120, 150]"
    assert a["movement"]["selected_count"] == body["movement"]["sample_count"] == 21 and a["movement"]["retained_count"] == 21 and a["movement"]["max_samples"] == 300
    assert body["movement"]["estimated_distance_m"] == pytest.approx(40.0) and body["movement"]["sources"] == {"reported": {}, "not_reported": 21}
    assert hr["before"]["sources"] == {"reported": {"simulated": 5}, "not_reported": 0}, "period sources preserved"


def test_effort_review_with_nothing_retained_and_with_retained_history_elsewhere(client, caps):
    caps(physiological=4, tracking=4)
    body = effort(client, at(100), at(110))
    for name in ("before", "during", "after"):
        assert body["availability"]["heart_rate"][name] == avail(max_samples=4)
    assert body["availability"]["movement"] == avail(max_samples=4) and body["movement"]["estimated_distance_m"] is None, "a missing distance stays null"
    for s in range(200, 206):                                   # six rows under a cap of four: trimmed to 202..205, outside the effort
        post_track(client, at(s)); post_phys(client, at(s))
    body = effort(client, at(100), at(110))
    m = body["availability"]["movement"]
    assert m == avail(max_samples=4, retained_count=4, retained_earliest=iso(202), retained_latest=iso(205), at_cap=True)
    assert body["movement"]["sample_count"] == 0 and body["movement"]["estimated_distance_m"] is None, "the effort has no samples; nothing becomes zero"
    assert body["availability"]["heart_rate"]["during"]["at_cap"] is True and body["heart_rate"]["during"]["sample_count"] == 0


def test_availability_model_rejects_negative_counts_and_unknown_fields():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        StreamAvailability.model_validate({**avail(), "retained_count": -1})
    with pytest.raises(ValidationError):
        StreamAvailability.model_validate({**avail(), "coverage": 0.5})
