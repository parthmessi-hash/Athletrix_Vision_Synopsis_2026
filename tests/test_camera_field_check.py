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


def test_bench_sequence_template_fit_check_and_report_without_a_detector(tmp_path):
    """The camera bench sequence in order, with NO detector model: template -> the operator fills in the measured
    values -> fit (no report needed first) -> bench (geometry only) -> course check using that fitted calibration.
    A wrong check point and a changed zoom fail; person tracking is reported as NOT EXERCISED, never as passed."""
    from camera_adapter import synthetic_people as sp
    from camera_adapter.evaluation import PEOPLE_CHECK_MARKS, PEOPLE_FIT_MARKS
    lines = []
    f = tmp_path / "FIELD"
    assert cfc.main(["template", "--out", str(f)], out=lines.append) == 0
    pts = json.loads((f / "points.json").read_text())
    assert cfc.main(["fit", "--points", str(f / "points.json"), "--out", str(f / "cal.json")], out=lines.append) == 2 and "ENTER placeholders" in lines[-1]
    cam = sp.Camera3()
    mark = lambda x, y: {"label": f"mark ({x}, {y})", "image": [round(v) for v in cam.project(x, y)], "field": [x, y]}  # noqa: E731
    settings = {"camera_id": "bench-cam", "zoom": "wide end", "focus": "manual 12 m", "mount": "tripod A"}
    pts.update(camera_id="bench-cam", image={"width": sp.WIDTH, "height": sp.HEIGHT}, camera_settings=settings,
               fit_points=[mark(*m) for m in PEOPLE_FIT_MARKS], check_points=[mark(*m) for m in PEOPLE_CHECK_MARKS])
    (f / "points.json").write_text(json.dumps(pts))
    (f / "now.json").write_text(json.dumps(settings))
    assert cfc.main(["fit", "--points", str(f / "points.json"), "--out", str(f / "cal.json")], out=lines.append) == 0 and "check points mark (12, 7)" in lines[-1]
    assert cfc.main(["bench", "--points", str(f / "points.json"), "--calibration", str(f / "cal.json"), "--now-settings", str(f / "now.json"),
                     "--max-check-error-m", "0.25", "--out", str(f / "bench")], out=lines.append) == 0
    b = json.loads((f / "bench" / "bench.json").read_text())
    assert b["geometry"]["verdict"] == "PASS" and b["source"]["verdict"] == "NOT CHECKED" and b["tracking"]["verdict"] == "NOT EXERCISED"
    assert "nothing about person detection or tracking was tested" in b["tracking"]["why"]
    # no tolerance stated: reported, not passed
    assert cfc.main(["bench", "--points", str(f / "points.json"), "--calibration", str(f / "cal.json"), "--out", str(f / "bench-nolimit")], out=lines.append) == 0
    assert json.loads((f / "bench-nolimit" / "bench.json").read_text())["geometry"]["verdict"] == "REPORTED"
    # the course check uses the fitted calibration; a calibration from other points is refused
    samples(tmp_path / "s.jsonl")
    (f / "course.json").write_text(json.dumps({"format": "athletrix-course", "format_version": 1, "axis": "x", "lane": {"y_min": 2.0, "y_max": 4.0},
                                               "marks": [{"label": "6 m", "at": 6.0}, {"label": "12 m", "at": 12.0}], "max_gap_s": 0.35}))
    assert cfc.main(["run", "--points", str(f / "points.json"), "--calibration", str(f / "cal.json"), "--course", str(f / "course.json"), "--samples", str(tmp_path / "s.jsonl"),
                     "--athlete", "vx1", "--now-settings", str(f / "now.json"), "--out", str(f / "report")], out=lines.append) == 0
    assert json.loads((f / "report" / "report.json").read_text())["intervals"][0]["average_speed_mps"] > 3
    other = dict(pts, fit_points=pts["fit_points"][:-1] + [mark(24, 13)])
    (f / "other.json").write_text(json.dumps(other))
    assert cfc.main(["run", "--points", str(f / "other.json"), "--calibration", str(f / "cal.json"), "--course", str(f / "course.json"), "--samples", str(tmp_path / "s.jsonl"),
                     "--athlete", "vx1", "--out", str(f / "report2")], out=lines.append) == 2 and "was not fitted from" in lines[-1]


def test_bench_example_shows_a_failed_check(tmp_path):
    lines = []
    assert cfc.main(["bench-example", "--out", str(tmp_path / "ex")], out=lines.append) == 0, lines
    ex = json.loads((tmp_path / "ex" / "examples.json").read_text())
    assert ex["good"]["geometry"] == "PASS" and ex["wrong-check-point"]["geometry"] == "FAIL" and ex["zoom-changed"]["geometry"] == "FAIL"
    assert all(v["tracking"] == "NOT EXERCISED" for v in ex.values())
    md = (tmp_path / "ex" / "wrong-check-point" / "bench.md").read_text()
    assert "INTENTIONALLY WRONG by 0.8 m): 0.79" in md and "3. Person detection and tracking: NOT EXERCISED" in md
