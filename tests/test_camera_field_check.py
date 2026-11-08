"""scripts/camera_field_check.py on SYNTHETIC samples drawn from the analytic truth of camera_adapter/synthetic_people.py
(no detector): check points in metres, an interval timed against the truth, a gap that excludes an interval with its
reason, average and peak speed kept apart, and a stale calibration (zoom or resolution change) refusing to time."""

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("camera_field_check", ROOT / "scripts" / "camera_field_check.py")
cfc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cfc)
A0 = datetime(2026, 11, 8, 10, 0, tzinfo=UTC)


def samples(path, gap=(5.4, 6.4)):
    from camera_adapter import synthetic_people as sp
    rows = []
    for k in range(200):
        t = k * 0.1
        if gap[0] < t < gap[1]:
            continue
        (x, y), _, _, v = sp.truth("red", t)
        rows.append({"type": "tracking", "sample": {"athlete_id": "vx1", "timestamp": (A0 + timedelta(seconds=t)).isoformat().replace("+00:00", "Z"), "x_position": x, "y_position": y,
                                                    "speed_mps": v, "acceleration_mps2": 0.0, "tracking_confidence": 0.9, "source": "simulated"}})
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_synthetic_field_check_times_one_interval_excludes_the_gap_and_flags_changes(tmp_path):
    samples(tmp_path / "s.jsonl")
    lines = []
    assert cfc.main(["synthetic", "--samples", str(tmp_path / "s.jsonl"), "--anchor-utc", "2026-11-08T10:00:00Z", "--out", str(tmp_path / "f")], out=lines.append) == 0, lines[-1]
    s = json.loads((tmp_path / "f" / "synthetic-summary.json").read_text())
    assert len(s["check_points_m"]) == 4 and all(p["error_m"] < 0.1 for p in s["check_points_m"])
    first, second = s["intervals_vs_truth"]
    assert abs(first["time_error_s"]) < 0.02 and abs(first["average_speed_error_mps"]) < 0.05
    assert "tracking gap of 1.00 s inside the interval" in second["excluded"]
    rep = json.loads((tmp_path / "f" / "report-same" / "report.json").read_text())
    timed = rep["intervals"][0]
    assert timed["peak_speed_accuracy"].startswith("NOT ESTABLISHED") and "average_speed_mps" in timed and timed["average_speed_mps"] != timed["peak_speed_mps_camera_estimate"]
    assert s["runs"]["zoom-changed"]["exit"] == 1 and "camera settings changed since calibration (zoom" in s["runs"]["zoom-changed"]["blocks"][0]
    assert s["runs"]["resolution-changed"]["exit"] == 1
    stale = json.loads((tmp_path / "f" / "report-zoom-changed" / "report.json").read_text())
    assert stale["intervals"] is None and stale["why_no_intervals"].startswith("calibration STALE or INCOMPATIBLE")
    md = (tmp_path / "f" / "report-same" / "report.md").read_text()
    assert "## Check points (independent)" in md and "accuracy not established" in md


def test_template_and_invalid_course(tmp_path):
    assert cfc.main(["template", "--out", str(tmp_path / "t")], out=lambda _: None) == 0
    assert cfc.main(["template", "--out", str(tmp_path / "t")], out=lambda _: None) == 2, "never overwrites"
    (tmp_path / "c.json").write_text(json.dumps({"format": "athletrix-course", "marks": [{"label": "a", "at": 0}]}))
    (tmp_path / "s.jsonl").write_text("")
    lines = []
    assert cfc.main(["run", "--points", str(tmp_path / "t" / "points.json"), "--course", str(tmp_path / "c.json"), "--samples", str(tmp_path / "s.jsonl"),
                     "--athlete", "x", "--out", str(tmp_path / "o")], out=lines.append) == 2 and lines[0].startswith("INVALID INPUT")
