"""The sprint detector and the camera verification report on SYNTHETIC inputs only: threshold crossing, a gap, noise
near the threshold, an identity switch, zero manual labels, uncertain identity, peak speed unmeasured."""

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.core.sprints import SprintRule, detect
from app.models import TrackingSample
from camera_adapter.verification import verify

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import camera_verify  # noqa: E402

T0 = datetime(2026, 11, 7, 12, tzinfo=UTC)


def s(t, v, seg="a", basis="estimated", conf=0.9):
    return TrackingSample(athlete_id="syn", timestamp=T0 + timedelta(seconds=t), x_position=0.0, y_position=0.0, speed_mps=v, acceleration_mps2=0.0,
                          tracking_confidence=conf, source="camera", segment_id=seg, value_basis=basis)


def series(speeds, step=0.1, start=0.0, seg="a"):
    return [s(start + k * step, v, seg) for k, v in enumerate(speeds)]


def test_threshold_crossing_starts_and_ends_a_sprint():
    sp = detect(series([4, 5, 5.6, 6.2, 6.8, 6.5, 5.9, 5.0, 4.0, 3.0]))
    assert len(sp) == 1 and sp[0]["samples"] == 5 and sp[0]["peak_speed_mps"] == 6.8 and sp[0]["ended_by"] == "speed_below_threshold"
    assert sp[0]["start"] == T0 + timedelta(seconds=0.2) and sp[0]["end"] == T0 + timedelta(seconds=0.6)
    assert detect(series([4, 5.6, 5.7, 4, 4])) == [], "two samples above the threshold are not a sprint (min_samples 3)"


def test_a_gap_ends_a_sprint_without_bridging():
    pts = series([5.8, 6.0, 6.2, 6.4]) + series([6.4, 6.2, 6.0, 5.8], start=1.5)
    sp = detect(pts)
    assert [x["ended_by"] for x in sp] == ["gap", "end_of_data"] and all(x["samples"] == 4 for x in sp)
    assert detect(series([5.8, 6.0]) + series([6.0], start=1.5)) == [], "samples either side of a gap never add up to a sprint"


def test_noise_near_the_threshold():
    hover = [5.4, 5.6, 5.4, 5.6, 5.4, 5.6, 5.4, 5.6]
    assert detect(series(hover)) == [], "flickering across the threshold never makes three consecutive samples"
    band = detect(series([6, 6.2, 6.1, 5.3, 6.0, 6.2, 4, 4]))
    assert len(band) == 1 and band[0]["samples"] == 6, "5.3 m/s lies in the hysteresis band (exit below 5.2): it stays part of the sprint"
    dip = series([6, 6.2, 6.1, 5.0, 6.0, 6.2, 4, 4])
    sp = detect(dip)
    assert len(sp) == 1 and sp[0]["samples"] == 5, "a one-sample dip below the exit level does not split a sprint (exit needs 2); the dip sample is left out"
    rule = SprintRule(threshold_mps=6.5)
    assert detect(dip, rule) == [], "the threshold is configurable"


def test_an_identity_switch_ends_a_sprint():
    pts = series([5.8, 6.0, 6.2, 6.4], seg="a") + series([6.4, 6.2, 6.0], start=0.4, seg="b")
    sp = detect(pts)
    assert [(x["segment_id"], x["ended_by"]) for x in sp] == [("a", "segment_change"), ("b", "end_of_data")]
    assert detect([s(k * 0.1, 7.0, basis="predicted") for k in range(5)]) == [], "predicted values never count"


def test_report_shows_false_detections_directly_and_excludes_uncertain_identity():
    pts = series([3.0] * 10 + [6, 6.5, 7, 6.5, 6] + [3.0] * 10)
    doc = {"fixture_kind": "synthetic", "event_windows": [{"athlete_id": "syn", "start": (T0).isoformat(), "end": (T0 + timedelta(seconds=3)).isoformat()}],
           "manual_events": [{"kind": "tackle", "athlete_id": "syn", "t": (T0 + timedelta(seconds=1)).isoformat(), "labeller": "synthetic"}],
           "course_runs": [{"athlete_id": "syn", "start": T0.isoformat(), "end": (T0 + timedelta(seconds=2.4)).isoformat(), "measured_distance_m": 10.0, "reference": "synthetic"}],
           "uncertain_identity": [{"athlete_id": "syn", "start": (T0 + timedelta(seconds=1.0)).isoformat(), "end": (T0 + timedelta(seconds=1.4)).isoformat()}]}
    r = verify(doc, pts)
    ev = r["events"][0]
    assert ev["sprint"]["manual"] == 0 and ev["sprint"]["count_error_pct"] is None and ev["sprint"]["detected"] == 0, "the uncertain interval removed the sprint samples"
    doc.pop("uncertain_identity")
    r = verify(doc, pts)
    ev = r["events"][0]
    assert len(ev["sprint"]["false_detections"]) == 1 and ev["sprint"]["count_error_pct"] is None and "false detection" in ev["sprint"]["note"]
    assert ev["tackle"]["manual"] == 1 and ev["tackle"]["automatic"].startswith("UNAVAILABLE")
    assert r["peak_speed"][0]["status"] == "UNMEASURED" and r["fixture_kind"] == "synthetic"


def test_synthetic_fixture_report(tmp_path):
    lines = []
    assert camera_verify.main(["make-synthetic", "--out", str(tmp_path / "f")], out=lines.append) == 0
    assert camera_verify.main(["report", str(tmp_path / "f" / "inputs.json"), "--out", str(tmp_path / "r")], out=lines.append) == 0
    r = json.loads((tmp_path / "r" / "results.json").read_text())
    c, sp, ev = r["course_runs"][0], r["speed"][0], r["events"][0]["sprint"]
    assert abs(c["error_pct"]) < 5 and c["within_target"] and abs(sp["error_pct"]) < 5 and "not ground truth" in sp["meaning"]
    assert (ev["manual"], ev["detected"], ev["matched"], ev["false_detections"]) == (2, 2, 2, [])
    assert "SYNTHETIC" in (tmp_path / "r" / "report.md").read_text()
