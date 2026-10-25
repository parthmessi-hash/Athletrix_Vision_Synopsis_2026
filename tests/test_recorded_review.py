"""Recorded-time review: the optional `end` parameter on the window and summary endpoints.

Live behaviour (no `end`) is unchanged. With `end`, that exact instant is the window end and the
start is `seconds` before it; selection, boundaries, ordering, tracking_rules and every calculation are
shared with the live path. Summary ages stay relative to window_end: in a review they are seconds
before the selected end, not age relative to now.
"""

from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import pytest

from app.core import clock

NOW = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)        # frozen backend time
REC = datetime(2026, 10, 20, 10, 0, 2, tzinfo=UTC)        # end of a fictional recording five days earlier
PHYS = "/api/v1/samples/physiological"
TRACK = "/api/v1/samples/tracking"


def at(base: datetime, seconds: float) -> datetime:
    return base + timedelta(seconds=seconds)


def url(kind, athlete_id="ath-1", seconds=60, end=None):
    u = f"/api/v1/athletes/{athlete_id}/{kind}?seconds={seconds}"
    return u if end is None else f"{u}&end={quote(end if isinstance(end, str) else end.isoformat())}"


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    monkeypatch.setattr(clock, "utc_now", lambda: NOW)


def post_phys(client, base, ts, hr=100.0, athlete_id="ath-1"):
    r = client.post(PHYS, json={**base, "athlete_id": athlete_id, "timestamp": ts.isoformat(), "heart_rate_bpm": hr})
    assert r.status_code == 200, r.text


def post_track(client, base, ts, speed=1.0, conf=1.0, athlete_id="ath-1"):
    r = client.post(TRACK, json={**base, "athlete_id": athlete_id, "timestamp": ts.isoformat(), "speed_mps": speed, "tracking_confidence": conf})
    assert r.status_code == 200, r.text


def ts_list(samples):
    return [datetime.fromisoformat(s["timestamp"]) for s in samples]


# ---- live defaults unchanged --------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["window", "summary"])
def test_without_end_the_window_ends_at_backend_time(client, kind, physiological_payload):
    post_phys(client, physiological_payload, at(NOW, -1))
    post_phys(client, physiological_payload, at(REC, 0))  # retained old recording, outside the live window
    body = client.get(url(kind)).json()
    assert datetime.fromisoformat(body["window_end"]) == NOW
    assert datetime.fromisoformat(body["window_start"]) == at(NOW, -60)
    if kind == "window":
        assert ts_list(body["physiological_samples"]) == [at(NOW, -1)]
    else:
        assert body["physiological"]["sample_count"] == 1 and body["physiological"]["latest_sample_age_seconds"] == 1.0


def test_live_clock_still_read_once(client, monkeypatch):
    calls = []
    def ticking():
        calls.append(1)
        return NOW + timedelta(seconds=len(calls))
    monkeypatch.setattr(clock, "utc_now", ticking)
    client.get(url("window", seconds=10)).json()
    assert len(calls) == 1
    calls.clear()
    client.get(url("summary", seconds=10, end=REC)).json()
    assert len(calls) == 1  # the future check reads the clock once; the recorded end itself does not


# ---- explicit historical end ----------------------------------------------------------------------------

def test_recorded_end_is_used_exactly_with_inclusive_boundaries(client, physiological_payload, tracking_payload):
    for s in (-10.001, -10, -5, 0, 0.001, 3):
        post_phys(client, physiological_payload, at(REC, s))
        post_track(client, tracking_payload, at(REC, s))
    body = client.get(url("window", seconds=10, end=REC)).json()
    assert datetime.fromisoformat(body["window_end"]) == REC
    assert datetime.fromisoformat(body["window_start"]) == at(REC, -10)
    assert ts_list(body["physiological_samples"]) == [at(REC, -10), at(REC, -5), at(REC, 0)]
    assert ts_list(body["tracking_samples"]) == [at(REC, -10), at(REC, -5), at(REC, 0)]
    assert body["tracking_rules"] == {"min_confidence": 0.5}


def test_recorded_end_excludes_newer_samples_and_keeps_order(client, physiological_payload):
    for s in (-1, -30, 5, 90):
        post_phys(client, physiological_payload, at(REC, s))
    post_phys(client, physiological_payload, at(NOW, -1))
    body = client.get(url("window", seconds=60, end=REC)).json()
    assert ts_list(body["physiological_samples"]) == [at(REC, -30), at(REC, -1)]


@pytest.mark.parametrize("seconds", [0, -1, 301, "x"])
def test_seconds_validation_unchanged_with_end(client, seconds):
    assert client.get(url("window", seconds=seconds, end=REC)).status_code == 422
    assert client.get(url("summary", seconds=seconds, end=REC)).status_code == 422


@pytest.mark.parametrize("seconds", [1, 300])
def test_seconds_bounds_accepted_with_end(client, seconds):
    body = client.get(url("window", seconds=seconds, end=REC)).json()
    assert datetime.fromisoformat(body["window_start"]) == at(REC, -seconds)


# ---- equivalent instants, different offsets -------------------------------------------------------------

@pytest.mark.parametrize("kind", ["window", "summary"])
def test_equivalent_offsets_select_the_same_samples(client, physiological_payload, kind):
    post_phys(client, physiological_payload, at(REC, -3), hr=90)
    post_phys(client, physiological_payload, at(REC, 0), hr=110)
    post_phys(client, physiological_payload, at(REC, 1), hr=130)
    utc = client.get(url(kind, seconds=10, end="2026-10-20T10:00:02Z")).json()
    plus2 = client.get(url(kind, seconds=10, end="2026-10-20T12:00:02+02:00")).json()
    minus5 = client.get(url(kind, seconds=10, end="2026-10-20T05:00:02-05:00")).json()
    for body in (plus2, minus5):
        assert datetime.fromisoformat(body["window_end"]) == datetime.fromisoformat(utc["window_end"]) == REC
        if kind == "window":
            assert body["physiological_samples"] == utc["physiological_samples"]
        else:
            assert body["physiological"] == utc["physiological"] and body["tracking"] == utc["tracking"]
    if kind == "summary":
        assert utc["physiological"]["heart_rate_mean_bpm"] == 100.0 and utc["physiological"]["sample_count"] == 2


# ---- invalid, naive and future ends -----------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["window", "summary"])
@pytest.mark.parametrize("bad", ["2026-10-20T10:00:02", "2026-10-20 10:00", "yesterday", "", "2026-13-01T00:00:00Z"])
def test_invalid_or_naive_end_422(client, kind, bad):
    r = client.get(url(kind, end=bad))
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail[0]["loc"] == ["query", "end"]


@pytest.mark.parametrize("kind", ["window", "summary"])
def test_future_end_422_with_clear_message(client, kind):
    r = client.get(url(kind, end=at(NOW, 1)))
    assert r.status_code == 422
    assert "after backend time" in r.json()["detail"] and "cannot end in the future" in r.json()["detail"]
    assert client.get(url(kind, end=NOW)).status_code == 200          # exactly now is allowed
    assert client.get(url(kind, end=at(NOW, -0.001))).status_code == 200


def test_unencoded_plus_is_rejected_not_misread(client):
    """A '+' left unencoded arrives as a space, which is not a valid timestamp; nothing is silently reinterpreted."""
    assert client.get("/api/v1/athletes/ath-1/window?seconds=60&end=2026-10-20T12:00:02+02:00").status_code == 422


# ---- historical summary calculations and ages --------------------------------------------------------------

def test_recorded_summary_calculations_and_ages_relative_to_selected_end(client, physiological_payload, tracking_payload):
    post_phys(client, physiological_payload, at(REC, -2), hr=112)
    post_phys(client, physiological_payload, at(REC, -0.5), hr=None)
    post_track(client, tracking_payload, at(REC, -2), speed=0.0, conf=0.9)
    post_track(client, tracking_payload, at(REC, -1), speed=1.5, conf=0.95)
    post_track(client, tracking_payload, at(REC, 0), speed=2.0, conf=1.0)
    post_track(client, tracking_payload, at(NOW, -1), speed=9.0)        # live sample: not part of the review
    s = client.get(url("summary", seconds=60, end=REC)).json()
    p, t = s["physiological"], s["tracking"]
    assert datetime.fromisoformat(s["window_end"]) == REC
    assert p["sample_count"] == 2 and p["heart_rate_count"] == 1 and p["heart_rate_mean_bpm"] == 112 and p["heart_rate_peak_bpm"] == 112
    assert p["latest_sample_age_seconds"] == 0.5          # seconds before the selected end, not ~5 days before now
    assert p["max_inter_sample_gap_seconds"] == 1.5
    assert t["sample_count"] == 3 and t["speed_peak_mps"] == 2.0 and t["latest_sample_age_seconds"] == 0.0
    assert t["estimated_distance_m"] == pytest.approx(2.5)  # (0+1.5)/2 + (1.5+2)/2
    assert t["included_duration_seconds"] == 2.0 and t["included_window_fraction"] == pytest.approx(2 / 60)
    assert t["high_speed_duration_seconds"] == 0.0 and t["high_speed_distance_m"] == 0.0
    assert t["distance_rules"]["min_confidence"] == 0.5
    live = client.get(url("summary", seconds=60)).json()["tracking"]
    assert live["sample_count"] == 1 and live["speed_peak_mps"] == 9.0 and live["estimated_distance_m"] is None


def test_summary_and_window_share_bounds_and_rules_for_the_same_end(client, tracking_payload):
    post_track(client, tracking_payload, at(REC, -1), conf=0.2)
    w = client.get(url("window", seconds=30, end=REC)).json()
    s = client.get(url("summary", seconds=30, end=REC)).json()
    assert (w["window_start"], w["window_end"]) == (s["window_start"], s["window_end"])
    assert w["tracking_rules"]["min_confidence"] == s["tracking"]["distance_rules"]["min_confidence"]
    assert w["tracking_samples"][0]["tracking_confidence"] == 0.2 and s["tracking"]["sample_count"] == 1


def test_empty_recorded_window(client, physiological_payload):
    post_phys(client, physiological_payload, at(REC, -120))
    w = client.get(url("window", seconds=60, end=REC)).json()
    s = client.get(url("summary", seconds=60, end=REC)).json()
    assert w["physiological_samples"] == [] and w["tracking_samples"] == [] and w["tracking_rules"] == {"min_confidence": 0.5}
    assert s["physiological"]["sample_count"] == 0 and s["physiological"]["latest_sample_age_seconds"] is None
    assert s["tracking"]["estimated_distance_m"] is None


# ---- athlete separation ---------------------------------------------------------------------------------

def test_recorded_review_keeps_athletes_separate(client, physiological_payload, tracking_payload):
    post_phys(client, physiological_payload, at(REC, -1), hr=100, athlete_id="ath-1")
    post_phys(client, physiological_payload, at(REC, -1), hr=150, athlete_id="ath-2")
    post_track(client, tracking_payload, at(REC, -1), speed=3.0, athlete_id="ath-2")
    a1 = client.get(url("window", "ath-1", 10, REC)).json()
    a2 = client.get(url("window", "ath-2", 10, REC)).json()
    assert [s["heart_rate_bpm"] for s in a1["physiological_samples"]] == [100] and a1["tracking_samples"] == []
    assert [s["heart_rate_bpm"] for s in a2["physiological_samples"]] == [150]
    assert [s["speed_mps"] for s in a2["tracking_samples"]] == [3.0]
    assert client.get(url("summary", "ath-1", 10, REC)).json()["tracking"]["sample_count"] == 0
    assert client.get(url("summary", "ath-2", 10, REC)).json()["physiological"]["heart_rate_peak_bpm"] == 150
    assert client.get(url("window", "nobody", 10, REC)).json()["physiological_samples"] == []
