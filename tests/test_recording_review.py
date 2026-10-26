"""Session review of a recording: recorded-data, window, summary, effort review and export read only that recording's samples.

The same calculations and rules as the rolling endpoints, no fallback to rolling history or another recording, no
retention cap, session availability instead, and the recording's own times kept apart from sample timestamps and
the requested period. All data is fictional.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.core import clock
from app.core.config import get_settings
from app.models import RecordedData, RecordingEffortReview, RecordingSummary, RecordingWindow

NOW = datetime(2026, 10, 26, 20, 0, 0, tzinfo=UTC)
REC = "/api/v1/recordings"
PHYS = "/api/v1/samples/physiological"
TRACK = "/api/v1/samples/tracking"
TRK = {"x_position": 0.0, "y_position": 0.0, "acceleration_mps2": 0.0, "tracking_confidence": 1.0}


@pytest.fixture
def frozen(monkeypatch):
    state = {"now": NOW}
    monkeypatch.setattr(clock, "utc_now", lambda: state["now"])
    return state


@pytest.fixture
def caps(monkeypatch):
    def configure(physiological, tracking):
        monkeypatch.setenv("PHYSIOLOGICAL_RETENTION_SAMPLES", str(physiological))
        monkeypatch.setenv("TRACKING_RETENTION_SAMPLES", str(tracking))
        get_settings.cache_clear()
    yield configure
    get_settings.cache_clear()


def at(seconds):
    return NOW + timedelta(seconds=seconds)


def iso(seconds):
    return at(seconds).isoformat().replace("+00:00", "Z")


def register(client, *ids):
    for n, a in enumerate(ids, start=1):
        assert client.post("/api/v1/athletes", json={"id": a, "name": f"Fictional {a}", "jersey_number": n, "sport": "fictional", "team_id": "t"}).status_code == 201


def start(client, ids, name="session"):
    r = client.post(REC, json={"name": name, "athlete_ids": list(ids)})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def phys(client, athlete, seconds, hr=100):
    assert client.post(PHYS, json={"athlete_id": athlete, "timestamp": at(seconds).isoformat(), "source": "simulated", "device_id": "d", "heart_rate_bpm": hr}).status_code == 200


def track(client, athlete, seconds, speed):
    assert client.post(TRACK, json={"athlete_id": athlete, "timestamp": at(seconds).isoformat(), "speed_mps": speed, **TRK}).status_code == 200


def session_url(rec, athlete, path):
    return f"{REC}/{rec}/athletes/{athlete}{path}"


@pytest.fixture
def scene(client, frozen, caps):
    """Rolling caps of 3; samples before the recording (rolling only), during it (both), then after it (rolling only); a second recording later."""
    caps(physiological=3, tracking=3)
    register(client, "a1", "a2")
    for s in range(-20, -15):
        phys(client, "a1", s, hr=50); track(client, "a1", s, 1.0)              # before the recording: rolling only
    frozen["now"] = at(-10)
    rec = start(client, ("a1", "a2"), name="first")
    for s in range(-10, 0):
        frozen["now"] = at(s)                                                      # backend time advances with the samples (skew rule unchanged)
        phys(client, "a1", s, hr=100 + (-s)); track(client, "a1", s, float(-s))  # recorded: ten samples each, 10 .. 1 m/s
    frozen["now"] = at(0)
    assert client.post(f"{REC}/{rec}/stop").status_code == 200
    for s in range(0, 5):
        frozen["now"] = at(s)
        phys(client, "a1", s, hr=200); track(client, "a1", s, 9.0)              # after the stop: rolling only
    rec2 = start(client, ("a1",), name="second")
    for s in range(5, 8):
        frozen["now"] = at(s)
        phys(client, "a1", s, hr=150); track(client, "a1", s, 2.0)              # another recording
    frozen["now"] = at(60)
    return {"rec": rec, "rec2": rec2}


def test_recorded_data_and_window_read_only_the_recordings_rows_without_caps(client, scene):
    rec = scene["rec"]
    rd = client.get(session_url(rec, "a1", "/recorded-data")).json()
    RecordedData.model_validate(rd)
    assert rd["recording"]["id"] == rec and rd["recording"]["status"] == "stopped" and rd["recording"]["started_at"] == iso(-10) and rd["recording"]["stopped_at"] == iso(0)
    assert rd["physiological"] == {"sample_count": 10, "earliest_timestamp": iso(-10), "latest_timestamp": iso(-1)} and rd["tracking"]["sample_count"] == 10
    assert client.get("/api/v1/athletes/a1/retained-data").json()["tracking"]["sample_count"] == 3, "the rolling store kept only its cap"
    assert client.get(session_url(rec, "a2", "/recorded-data")).json()["physiological"]["sample_count"] == 0
    win = client.get(session_url(rec, "a1", "/window"), params={"seconds": 60, "end": iso(10)}).json()
    RecordingWindow.model_validate(win)
    assert [s["speed_mps"] for s in win["tracking_samples"]] == [10.0, 9.0, 8.0, 7.0, 6.0, 5.0, 4.0, 3.0, 2.0, 1.0], "all ten recorded samples, none from before, after or the second recording"
    assert [s["heart_rate_bpm"] for s in win["physiological_samples"]] == [110, 109, 108, 107, 106, 105, 104, 103, 102, 101]
    assert win["window_start"] == iso(-50) and win["window_end"] == iso(10) and win["recording"]["started_at"] == iso(-10), "requested period and recording times are reported apart"
    assert win["availability"] is None and win["session_availability"]["tracking"] == {"recorded_count": 10, "recorded_earliest": iso(-10), "recorded_latest": iso(-1), "selected_count": 10, "selected_first": iso(-10), "selected_last": iso(-1)}
    assert win["tracking_rules"] == {"min_confidence": 0.5} and win["sources"]["physiological"] == {"reported": {"simulated": 10}, "not_reported": 0}
    partial = client.get(session_url(rec, "a1", "/window"), params={"seconds": 3, "end": iso(-5)}).json()
    assert [s["speed_mps"] for s in partial["tracking_samples"]] == [8.0, 7.0, 6.0, 5.0] and partial["session_availability"]["tracking"]["selected_count"] == 4, "inclusive bounds [-8, -5], as in the rolling window"
    empty = client.get(session_url(rec, "a1", "/window"), params={"seconds": 10, "end": iso(30)}).json()
    assert empty["tracking_samples"] == [] and empty["session_availability"]["tracking"] == {"recorded_count": 10, "recorded_earliest": iso(-10), "recorded_latest": iso(-1), "selected_count": 0, "selected_first": None, "selected_last": None}
    other = client.get(session_url(scene["rec2"], "a1", "/window"), params={"seconds": 60, "end": iso(10)}).json()
    assert [s["speed_mps"] for s in other["tracking_samples"]] == [2.0, 2.0, 2.0], "the second recording sees only its own rows"


def test_summary_and_effort_review_reuse_the_rolling_calculations_exactly(client, scene):
    rec = scene["rec"]
    s = client.get(session_url(rec, "a1", "/summary"), params={"seconds": 60, "end": iso(10)}).json()
    RecordingSummary.model_validate(s)
    assert s["tracking"]["sample_count"] == 10 and s["tracking"]["speed_peak_mps"] == 10.0 and s["tracking"]["estimated_distance_m"] == pytest.approx(49.5), "trapezoid over nine 1 s intervals"
    assert s["physiological"]["heart_rate_mean_bpm"] == pytest.approx(105.5) and s["session_availability"]["physiological"]["selected_count"] == s["physiological"]["sample_count"] == 10
    assert s["tracking"]["distance_rules"]["high_speed_threshold_mps"] == 5.0 and s["tracking"]["high_speed_duration_seconds"] == pytest.approx(5.0)
    assert s["availability"] is None and s["recording"]["name"] == "first"
    e = client.get(session_url(rec, "a1", "/effort-review"), params={"effort_start": iso(-8), "effort_end": iso(-3)}).json()
    RecordingEffortReview.model_validate(e)
    assert e["movement"]["sample_count"] == 6 and e["movement"]["estimated_distance_m"] == pytest.approx(27.5) and e["effort_duration_seconds"] == 5.0
    hr = e["heart_rate"]
    assert hr["before"]["sample_count"] == 2 and hr["during"]["sample_count"] == 6 and hr["after"]["sample_count"] == 2, "before [-18,-8) holds -10,-9; after (-3, 27] holds -2,-1 only: rolling samples after the stop are not visible"
    assert hr["after"]["heart_rate_mean_bpm"] == pytest.approx(101.5) and e["follow_up_elapsed"] is True
    sa = e["session_availability"]
    assert sa["movement"]["selected_count"] == 6 and sa["heart_rate"]["after"] == {"recorded_count": 10, "recorded_earliest": iso(-10), "recorded_latest": iso(-1), "selected_count": 2, "selected_first": iso(-2), "selected_last": iso(-1)}
    assert e["availability"] is None and e["recording"]["stopped_at"] == iso(0)
    # the same effort against the rolling endpoint sees different samples (its cap and the later ingestion), so the two are kept apart by design
    rolling = client.get("/api/v1/athletes/a1/effort-review", params={"effort_start": iso(-8), "effort_end": iso(-3)}).json()
    assert rolling["movement"]["sample_count"] == 0 and rolling["movement"]["estimated_distance_m"] is None
    # an athlete of the recording without any recorded samples: movement empty, distance null, heart rate periods empty
    e2 = client.get(session_url(rec, "a2", "/effort-review"), params={"effort_start": iso(-8), "effort_end": iso(-3)}).json()
    assert e2["movement"]["sample_count"] == 0 and e2["movement"]["estimated_distance_m"] is None and e2["heart_rate"]["during"]["sample_count"] == 0
    assert e2["session_availability"]["movement"] == {"recorded_count": 0, "recorded_earliest": None, "recorded_latest": None, "selected_count": 0, "selected_first": None, "selected_last": None}


def test_query_validation_and_404s_are_preserved(client, scene):
    rec = scene["rec"]
    assert client.get(session_url(rec, "a1", "/window"), params={"seconds": 301}).status_code == 422
    assert client.get(session_url(rec, "a1", "/window"), params={"seconds": 60, "end": iso(120)}).status_code == 422, "a future end is rejected"
    assert client.get(session_url(rec, "a1", "/window"), params={"seconds": 60, "end": "2026-10-26T20:00:00"}).status_code == 422, "naive timestamps are rejected"
    assert client.get(session_url(rec, "a1", "/effort-review"), params={"effort_start": iso(-8)}).status_code == 422
    assert client.get(session_url(rec, "a1", "/effort-review"), params={"effort_start": "1761508800", "effort_end": iso(-3)}).status_code == 422
    assert client.get(session_url(rec, "a1", "/effort-review"), params={"effort_start": iso(-3), "effort_end": iso(-8)}).status_code == 422
    for path in ("/recorded-data", "/window", "/summary", "/recorded-samples.jsonl"):
        assert client.get(session_url("rec-nope", "a1", path)).status_code == 404, path
        assert client.get(session_url(rec, "a9", path)).status_code == 404, "not part of the recording: " + path
    assert client.get(session_url(rec, "a9", "/effort-review"), params={"effort_start": iso(-8), "effort_end": iso(-3)}).status_code == 404
    live = client.get(session_url(rec, "a1", "/window")).json()
    assert live["window_end"] == iso(60) and live["tracking_samples"] == [], "without end the window ends at backend time now, as for the rolling window"


def test_export_holds_every_recorded_sample_as_stored_with_a_safe_filename(client, scene, isolated_db):
    rec = scene["rec"]
    r = client.get(session_url(rec, "a1", "/recorded-samples.jsonl"))
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson")
    records = [json.loads(line) for line in r.text.splitlines()]
    assert len(records) == 20 and r.headers["X-Athletrix-Record-Count"] == "20" and r.headers["X-Athletrix-Physiological-Count"] == "10" and r.headers["X-Athletrix-Tracking-Count"] == "10"
    assert r.headers["X-Athletrix-Recording-Id"] == rec and "X-Athletrix-Recording-Id" in r.headers["Access-Control-Expose-Headers"]
    assert r.headers["Content-Disposition"] == f'attachment; filename="athletrix-recording-{rec}-a1-20261026T200100Z.jsonl"'
    assert [x["type"] for x in records[:2]] == ["physiological", "tracking"] and records[0]["sample"]["timestamp"] == iso(-10)
    with isolated_db.read() as c:
        stored = [json.loads(row["payload"]) for row in c.execute("SELECT payload FROM recording_tracking_samples WHERE recording_id = ? AND athlete_id = 'a1' ORDER BY ts_utc", (rec,)).fetchall()]
    assert [x["sample"] for x in records if x["type"] == "tracking"] == stored, "written exactly as stored"
    assert all(set(x) == {"type", "sample"} for x in records), "the interchange format carries no roster or session metadata"
    assert client.get(session_url(rec, "a2", "/recorded-samples.jsonl")).status_code == 404
    from app.api.recordings import recording_export_filename
    assert recording_export_filename("rec/../x", "a b", NOW) == "athletrix-recording-rec_.._x-a_b-20261026T200000Z.jsonl"
    assert recording_export_filename("", "", NOW) == "athletrix-recording-recording-athlete-20261026T200000Z.jsonl"
    assert "/" not in recording_export_filename("rec/../x", "a/b", NOW) and "\\" not in recording_export_filename("r\\x", "a", NOW)
