"""Continuity segments (TrackingSample.segment_id): a provider-declared discontinuity is never integrated across.

Covers the pure distance rule, the camera adapter's segment labels (gaps, track changes, assignment conflicts), legacy
streams without labels, and preservation through ingestion, storage, windows, recordings, retained export and a
package export/import round trip. All data is fictional.
"""

import importlib.util
import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core import clock
from app.core.distance import continuity_breaks, estimate_distance, estimate_high_speed
from app.db import Database, set_database
from app.main import create_app
from app.models import TrackingSample
from camera_adapter import calibration as cal
from camera_adapter.observations import Observation, ObservationFile, Video
from camera_adapter.pipeline import AdapterConfig, TimeMapping, process, records

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("recording_package_cli_continuity", ROOT / "scripts" / "recording_package.py")
pkg_cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(pkg_cli)

T0 = datetime(2026, 10, 26, 20, 0, 0, tzinfo=UTC)
REC = "/api/v1/recordings"


def at(s):
    return T0 + timedelta(seconds=s)


def z(s):
    return at(s).isoformat().replace("+00:00", "Z")


def ts(t, v, seg=None, conf=1.0, athlete="a1"):
    return TrackingSample(athlete_id=athlete, timestamp=at(t), x_position=0, y_position=0, speed_mps=v, acceleration_mps2=0, tracking_confidence=conf, segment_id=seg)


# ---- the pure rule ------------------------------------------------------------------------------------------------------

def test_continuous_eligible_motion_in_one_segment_still_counts():
    d = estimate_distance([ts(0, 2.0, "s1"), ts(2, 4.0, "s1")], 2.0)
    assert d.estimated_distance_m == 6.0 and d.included_duration_seconds == 2.0 and d.excluded_interval_count == 0 and d.continuity_break_count == 0


def test_a_declared_discontinuity_contributes_nothing_however_short():
    samples = [ts(0, 2.0, "s1"), ts(1, 2.0, "s1"), ts(1.5, 4.0, "s2"), ts(2.5, 4.0, "s2")]
    d = estimate_distance(samples, 2.5)
    assert d.estimated_distance_m == 2.0 + 4.0 and d.included_duration_seconds == 2.0   # 0.5 s across the break is not counted
    assert d.excluded_interval_count == 1 and d.continuity_break_count == 1
    hs = estimate_high_speed(samples, threshold_mps=3.0)
    assert hs.high_speed_duration_seconds == 1.0 and hs.high_speed_distance_m == 4.0   # only the s2 interval, never the crossing into it


def test_a_labelled_sample_next_to_an_unlabelled_one_is_a_break_and_breaks_are_a_subset_of_exclusions():
    samples = [ts(0, 1.0), ts(1, 1.0), ts(2, 1.0, "s1"), ts(3, 1.0, "s1", conf=0.2), ts(10, 1.0, "s1")]
    d = estimate_distance(samples, 10)
    assert d.estimated_distance_m == 1.0 and d.excluded_interval_count == 3 and d.continuity_break_count == 1 == continuity_breaks(samples)


def test_streams_without_continuity_metadata_keep_the_documented_gap_and_confidence_rules():
    samples = [ts(0, 2.0), ts(1, 2.0), ts(2.5, 2.0), ts(6, 2.0)]    # a 1.5 s gap is joined (<= 3 s); 3.5 s is not
    d = estimate_distance(samples, 6)
    assert d.estimated_distance_m == 5.0 and d.excluded_interval_count == 1 and d.continuity_break_count == 0
    assert "segment_id" not in samples[0].model_dump(mode="json") and "segment_id" not in json.loads(samples[0].model_dump_json())


def test_segment_id_is_validated():
    with pytest.raises(ValueError):
        ts(0, 1.0, "")
    with pytest.raises(ValueError):
        ts(0, 1.0, "x" * 129)


# ---- the camera adapter's labels ----------------------------------------------------------------------------------------

H = [[18.0, 4.0, 120.0], [0.5, -9.0, 420.0], [0.0, 0.004, 1.0]]
FIELD = [(0, 0), (40, 0), (40, 20), (0, 20), (20, 10), (10, 15)]


def image(x, y):
    u, v, _ = cal.apply_h(H, x, y)
    return u, v


@pytest.fixture
def calibration():
    pts = [{"label": f"p{i}", "image": list(image(*f)), "field": list(f)} for i, f in enumerate(FIELD)]
    return cal.fit(960, 540, pts, [], camera_id="test-cam")


def walk(frames, *, track=lambda f: "trk-1", athlete="ath-1", speed=2.0, fps=10.0):
    """An athlete walking along x at a constant speed, observed in the given frames."""
    obs = []
    for f in frames:
        u, v = image(5.0 + speed * f / fps, 8.0)
        obs.append(Observation(f, f / fps, track(f), u, v, 0.9, athlete))
    return obs


def run(calibration, obs, **cfg):
    config = AdapterConfig(**cfg)
    res = process(ObservationFile(Video(960, 540, 10.0, "t"), "test", obs), calibration, config)
    mapping = TimeMapping(T0)
    return res, [TrackingSample.model_validate(r["sample"]) for r in records(res, mapping, config, "tag")]


def test_an_interruption_longer_than_the_estimator_gap_starts_a_new_segment_and_is_not_counted(calibration):
    frames = [f for f in range(0, 80) if not 30 <= f < 40]     # a 1 s camera interruption (never observed) at 3-4 s
    res, samples = run(calibration, walk(frames))
    assert [s["started_by"] for s in res.segments] == ["first_sample", "gap"]
    labels = [s.segment_id for s in samples]
    assert labels[0] == "cam-tag-1" and labels[-1] == "cam-tag-2"
    d = estimate_distance(samples, 8)
    assert d.continuity_break_count == 1
    gap = [(a, b) for a, b in zip(samples, samples[1:]) if a.segment_id != b.segment_id][0]
    assert (gap[1].timestamp - gap[0].timestamp).total_seconds() > 1.0                       # within the engine's 3 s rule ...
    assert math.isclose(d.included_duration_seconds, sum((b.timestamp - a.timestamp).total_seconds() for a, b in zip(samples, samples[1:]) if a.segment_id == b.segment_id))
    # ... and without labels (documented previous behaviour) the engine would have counted across it
    _, legacy = run(calibration, walk(frames), emit_segment_id=False)
    assert all(s.segment_id is None for s in legacy)
    dl = estimate_distance(legacy, 8)
    assert dl.continuity_break_count == 0 and dl.estimated_distance_m > d.estimated_distance_m + 2.0


def test_continuous_observation_keeps_one_segment(calibration):
    res, samples = run(calibration, walk(range(0, 60)))
    assert len(res.segments) == 1 and {s.segment_id for s in samples} == {"cam-tag-1"}
    d = estimate_distance(samples, 6)
    assert d.continuity_break_count == 0 and abs(d.estimated_distance_m - 2.0 * d.included_duration_seconds) < 1e-6


def test_a_track_change_never_joins_the_two_tracks(calibration):
    res, samples = run(calibration, walk(range(0, 60), track=lambda f: "trk-1" if f < 30 else "trk-2"))
    assert [s["started_by"] for s in res.segments] == ["first_sample", "track_change"]
    by_seg = {}
    for o in res.outputs:
        by_seg.setdefault(o.segment, set()).add(o.track_id)
    assert by_seg == {1: {"trk-1"}, 2: {"trk-2"}}
    assert estimate_distance(samples, 6).continuity_break_count == 1


def test_an_assignment_conflict_breaks_the_segment(calibration):
    obs = walk(range(0, 60))
    ou, ov = image(30.0, 15.0)
    obs += [Observation(f, f / 10.0, "trk-9", ou, ov, 0.9, "ath-1") for f in (30, 31)]   # a second track claimed for the same athlete
    obs.sort(key=lambda o: (o.frame, o.track_id))
    res, samples = run(calibration, obs)
    assert "assignment_conflict" in [s["started_by"] for s in res.segments]
    assert all(o.track_id == "trk-1" for o in res.outputs) and estimate_distance(samples, 6).continuity_break_count == 1


def test_a_suppressed_estimate_breaks_the_segment_but_rate_limiting_does_not(calibration):
    obs = walk(range(0, 120), fps=20.0)    # 20 fps observations, at most 10 samples per second: rate limiting skips half
    u, v = image(25.0, 15.0)        # one wild jump: the quadratic fit's residual exceeds max_fit_rms_m for a while
    obs[60] = Observation(60, 3.0, "trk-1", u, v, 0.9, "ath-1")
    res, samples = run(calibration, obs)
    assert res.skipped.get("rate_limit", 0) > 0 and res.skipped.get("fit_residual", 0) > 0
    assert [s["started_by"] for s in res.segments] == ["first_sample", "fit_residual"]


# ---- through the engine: ingestion, storage, recording, export and import -------------------------------------------------

@pytest.fixture
def frozen(monkeypatch):
    state = {"now": T0}
    monkeypatch.setattr(clock, "utc_now", lambda: state["now"])
    return state


def body(t, v, seg):
    s = {"athlete_id": "a1", "timestamp": z(t), "x_position": 0.0, "y_position": 0.0, "speed_mps": v, "acceleration_mps2": 0.0, "tracking_confidence": 1.0, "source": "simulated"}
    return {**s, "segment_id": seg} if seg else s


def test_segments_survive_ingestion_storage_recording_retained_export_and_package_round_trip(client, frozen, tmp_path):
    assert client.post("/api/v1/athletes", json={"id": "a1", "name": "Fictional a1", "jersey_number": 1, "sport": "soccer", "team_id": "t"}).status_code == 201
    rec = client.post(REC, json={"name": "continuity", "athlete_ids": ["a1"]}).json()["id"]
    plan = [(0, 2.0, "s1"), (1, 2.0, "s1"), (2, 2.0, "s1"), (3, 4.0, "s2"), (4, 4.0, "s2"), (5, 1.0, None), (6, 1.0, None)]
    for t, v, seg in plan:
        frozen["now"] = at(t)
        r = client.post("/api/v1/samples/tracking", json=body(t, v, seg))
        assert r.status_code == 200, r.text
    frozen["now"] = at(8)
    window = client.get("/api/v1/athletes/a1/window", params={"seconds": 60}).json()
    assert [s.get("segment_id") for s in window["tracking_samples"]] == [p[2] for p in plan]
    assert all("segment_id" not in s for s in window["tracking_samples"][5:])          # unlabelled samples stay unlabelled
    summary = client.get("/api/v1/athletes/a1/summary", params={"seconds": 60}).json()["tracking"]
    assert summary["continuity_break_count"] == 2 and summary["excluded_interval_count"] == 2 and summary["estimated_distance_m"] == 4.0 + 4.0 + 1.0
    assert summary["distance_rules"]["continuity_rule"].startswith("never join")
    retained = client.get("/api/v1/athletes/a1/retained-samples.jsonl").text.splitlines()
    assert [json.loads(x)["sample"].get("segment_id") for x in retained] == [p[2] for p in plan]
    client.post(f"{REC}/{rec}/stop")
    recorded = client.get(f"{REC}/{rec}/athletes/a1/recorded-samples.jsonl").text.splitlines()
    assert [json.loads(x)["sample"].get("segment_id") for x in recorded] == [p[2] for p in plan]
    review = client.get(f"{REC}/{rec}/athletes/a1/effort-review", params={"effort_start": z(0), "effort_end": z(6)}).json()["movement"]
    assert review["continuity_break_count"] == 2 and review["estimated_distance_m"] == 9.0
    pkg = tmp_path / "pkg.zip"
    pkg.write_bytes(client.get(f"{REC}/{rec}/package.zip").content)
    dest = tmp_path / "dest.db"
    lines = []
    assert pkg_cli.main(["import", str(pkg), "--database", str(dest), "--json"], out=lines.append, now=lambda: at(3600)) == 0, lines
    new_id = json.loads("\n".join(lines))["recording_id"]
    db = Database(dest); set_database(db)
    try:
        c = TestClient(create_app())
        again = c.get(f"{REC}/{new_id}/athletes/a1/recorded-samples.jsonl").text.splitlines()
        assert again == recorded
        review2 = c.get(f"{REC}/{new_id}/athletes/a1/effort-review", params={"effort_start": z(0), "effort_end": z(6)}).json()["movement"]
        assert review2["continuity_break_count"] == 2 and review2["estimated_distance_m"] == 9.0
    finally:
        db.close(); set_database(None)


def test_an_equal_instant_replacement_replaces_the_segment_label_with_the_values(client, frozen):
    assert client.post("/api/v1/athletes", json={"id": "a1", "name": "Fictional a1", "jersey_number": 1, "sport": "soccer", "team_id": "t"}).status_code == 201
    frozen["now"] = at(1)
    assert client.post("/api/v1/samples/tracking", json=body(0, 2.0, "s1")).status_code == 200
    assert client.post("/api/v1/samples/tracking", json=body(0, 3.0, None)).status_code == 200
    latest = client.get("/api/v1/athletes/a1/tracking/latest").json()
    assert latest["speed_mps"] == 3.0 and "segment_id" not in latest


def test_estimator_evaluation_uses_analytic_truth_and_reports_unavailable_time_and_phantom_distance():
    from camera_adapter import evaluation as ev  # plain Python; the full comparison is scripts/camera_synthetic_check.py estimator
    result = ev.evaluate_config(ev.CONFIGS[ev.DEFAULT_NAME], "development")
    rows = {(r["scenario"], r["variant"]): r for r in result["rows"]}
    clean = rows[("stationary", "clean")]
    assert clean["distance"]["estimated_distance_m"] == 0.0 and clean["speed_mps"]["max_abs"] < 1e-6 and clean["first_sample_s"] == pytest.approx(0.32)
    assert rows[("stationary", "noise 1.5 px")]["distance"]["estimated_distance_m"] > 1.0      # pixel noise inflates speed: a documented limitation
    gap = rows[("constant speed, near", "1.0 s gap")]
    assert gap["distance"]["continuity_breaks"] == 1 and gap["unavailable_s"] > 1.0
    assert rows[("constant speed, near", "track change")]["segments"] == 2
    summary = ev.summarize(result)
    assert summary["lag_never_settled_clean"] == 0 and summary["throughput_observations_per_s"] > 0
    sel = ev.select({name: summary if name == ev.DEFAULT_NAME else {**summary, "speed_steady_p95_mps": summary["speed_steady_p95_mps"] / 2} for name in ev.CONFIGS})
    assert sel["decision"] == ev.DEFAULT_NAME     # better on noise alone is not enough under the fixed rule
