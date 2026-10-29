"""Synthetic camera check: generated video -> synthetic marker tracking -> calibration -> movement estimates -> errors.

Needs OpenCV and NumPy (pip install -r requirements-vision.txt); runs offline. Fictional data only.

STEPS
  1. Render a 30 s, 20 fps, 960x540 MJPG video of a fictional 40 x 20 m field seen by a virtual pinhole camera, with
     three synthetic disc markers (camera_adapter/synthetic_truth.py): red and blue move through stationary,
     constant-speed, accelerating and decelerating phases (blue also along a 4 m curve), both at once for several
     seconds; red passes behind a grey occluder; blue is deliberately not drawn for 0.2 s and 0.6 s; yellow appears
     later and is never assigned.
  2. "Measure" calibration points the way an operator clicking pixels would: true image positions rounded to whole
     pixels. Six field marks fit the homography; four other marks are independent check points.
  3. Extract observations with synthetic marker tracking (colour blobs; not person detection).
  4. Assign tracks to two fictional athletes explicitly (the operator's step): red tracks -> fictional-athlete-01;
     blue tracks -> fictional-athlete-02, the first blue track only from 1.5 s (an unassigned interval); yellow never.
  5. Run the adapter (scripts/camera_to_samples.py export) with the example configuration and a stated anchor.
  6. Compare every produced sample with the analytic truth at its video time.

REPORTED (no acceptance threshold is applied to measured errors; they are measurements of this synthetic setup)
  position, speed and acceleration errors (acceleration only where the estimator's window contains no change of the
  true acceleration; samples near changes are reported separately as boundary effects), settle times after each
  change, valid outputs, warm-up losses, skips by reason, estimator resets, unassigned intervals, identity
  separation, how many samples fall below the engine's default 0.5 distance-confidence limit, and the difference
  between this adapter's homography and OpenCV's findHomography on the same points.
CHECKED (pass/fail): no sample for an unassigned track, no sample nearer another target than its own (no identity
  swap), no sample whose estimator window spans a gap longer than max_gap_s, at most 10 samples per second per athlete.

Synthetic success says nothing about athletes, real footage, lighting, occlusion by people, lens distortion or a full
field. Writes the report to reports/ (ignored by git); generated files go to a temporary folder unless --workdir.

USAGE
    python scripts/camera_synthetic_check.py [--workdir DIR] [--report PATH]
"""

import argparse
import json
import math
import statistics
import sys
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import camera_to_samples as adapter_cli  # noqa: E402
import camera_calibrate  # noqa: E402
from camera_adapter import synthetic_truth as st  # noqa: E402
from camera_adapter.observations import dump  # noqa: E402

ATHLETE = {"red": "fictional-athlete-01", "blue": "fictional-athlete-02"}
FIT_MARKS = [(0, 0), (40, 0), (40, 20), (0, 20), (20, 0), (20, 20)]
CHECK_MARKS = [(10, 10), (30, 10), (20, 5), (20, 15)]
ANCHOR = "2026-10-20T10:00:00Z"
EXAMPLE_CONFIG = ROOT / "scripts" / "examples" / "camera" / "adapter_config_example.json"


def stats(values: list[float]) -> dict | None:
    v = sorted(abs(x) for x in values)
    if not v:
        return None
    return {"n": len(v), "mean_abs": round(statistics.fmean(v), 4), "median_abs": round(v[len(v) // 2], 4), "p95_abs": round(v[min(len(v) - 1, math.ceil(0.95 * len(v)) - 1)], 4), "max_abs": round(v[-1], 4)}


def run(workdir: Path, out: Callable[[str], None]) -> dict:
    import cv2  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415

    from camera_adapter.synthetic_video import extract, render_video  # noqa: PLC0415
    camera = st.Camera()
    video = workdir / "synthetic-markers.avi"
    render_video(video, camera)

    def mark(x, y):
        u, v = camera.project(x, y)
        return {"label": f"mark ({x}, {y})", "image": [round(u), round(v)], "field": [x, y]}
    points = {"format": "athletrix-calibration-points", "format_version": 1, "camera_id": "synthetic-virtual-camera", "image": {"width": st.WIDTH, "height": st.HEIGHT},
              "fit_points": [mark(*m) for m in FIT_MARKS], "check_points": [mark(*m) for m in CHECK_MARKS]}
    (workdir / "calibration_points.json").write_text(json.dumps(points, indent=2))
    lines: list[str] = []
    assert camera_calibrate.main(["fit", str(workdir / "calibration_points.json"), "--out", str(workdir / "calibration.json")], out=lines.append) == 0, lines
    calib = json.loads((workdir / "calibration.json").read_text())

    obs, colour_of = extract(video)
    dump(obs, workdir / "observations.jsonl")
    first_blue = min((t for t, c in colour_of.items() if c == "blue"), key=lambda t: int(t.split("-")[1]))
    assignments = [{"track_id": t, "athlete_id": ATHLETE[c], "from_frame": int(1.5 * st.FPS) if t == first_blue else 0, "assigned_by": "operator step simulated by the synthetic check"}
                   for t, c in sorted(colour_of.items()) if c in ATHLETE]
    (workdir / "assignments.json").write_text(json.dumps({"format": "athletrix-track-assignments", "format_version": 1, "assignments": assignments}, indent=2))
    colour_by_athlete = {v: k for k, v in ATHLETE.items()}

    def assess(cfg_path: Path, tag: str):
        """Run the adapter with one configuration and compare every sample with the analytic truth."""
        code = adapter_cli.main(["export", str(workdir / "observations.jsonl"), "--calibration", str(workdir / "calibration.json"), "--config", str(cfg_path),
                                 "--assignments", str(workdir / "assignments.json"), "--anchor-utc", ANCHOR, "--out", str(workdir / f"samples{tag}.jsonl"), "--log", str(workdir / f"adapter-log{tag}.json")], out=lines.append)
        assert code == 0, lines
        log = json.loads((workdir / f"adapter-log{tag}.json").read_text())
        samples = [json.loads(x)["sample"] for x in (workdir / f"samples{tag}.jsonl").read_text().splitlines()]
        window = json.loads(cfg_path.read_text())["estimator"]["window_s"]
        errors = {a: {"position": [], "speed": [], "speed_boundary": [], "speed_after_restart": [], "accel_steady": [], "accel_boundary": [], "accel_after_restart": []} for a in ATHLETE.values()}
        restarts = {a: [r["video_time_s"] for r in log["resets"] if r["athlete_id"] == a] for a in ATHLETE.values()}
        swaps, per_sample = 0, []
        for meta, s in zip(log["samples"], samples):
            a, t = meta["athlete_id"], meta["video_time_s"]
            colour = colour_by_athlete[a]
            pos, vel, acc, speed = st.truth(colour, t)
            e_pos = math.dist((s["x_position"], s["y_position"]), pos)
            others = [math.dist((s["x_position"], s["y_position"]), st.truth(c, t)[0]) for c in ("red", "blue", "yellow") if c != colour and st.truth(c, t)]
            if others and min(others) < e_pos:
                swaps += 1
            near_change = any(t - window - 1e-9 <= c <= t + 1e-9 for c in st.ACCEL_CHANGES[colour])
            after_restart = any(r <= t <= r + window + 1e-9 for r in restarts[a])   # first window after a gap reset
            bucket = "after_restart" if after_restart else ("boundary" if near_change else None)
            errors[a]["position"].append(e_pos)
            errors[a][f"speed_{bucket}" if bucket else "speed"].append(s["speed_mps"] - speed)
            errors[a][f"accel_{bucket}" if bucket else "accel_steady"].append(s["acceleration_mps2"] - acc)
            per_sample.append((a, t, s["speed_mps"] - speed, s["acceleration_mps2"] - acc))
        return log, samples, errors, swaps, per_sample

    log, samples, errors, swaps, per_sample = assess(EXAMPLE_CONFIG, "")
    cfg = json.loads(EXAMPLE_CONFIG.read_text())
    window, max_gap = cfg["estimator"]["window_s"], cfg["estimator"]["max_gap_s"]
    longer = {**cfg, "estimator": {**cfg["estimator"], "window_s": 1.0, "min_span_s": 0.6}}
    (workdir / "adapter-config-window-1s.json").write_text(json.dumps(longer, indent=2))
    _, samples_1s, errors_1s, swaps_1s, per_sample_1s = assess(workdir / "adapter-config-window-1s.json", "-window-1s")

    def settle(a, change, key, tol, rows=None):
        later = [(t, ds, da) for aa, t, ds, da in (rows if rows is not None else per_sample) if aa == a and t >= change]
        for i, (t, ds, da) in enumerate(later):
            if all(abs(x[1 if key == "speed" else 2]) <= tol for x in later[i:i + 3]):
                return round(t - change, 3)
        return None
    delays = {a: [{"change_s": c, "speed_within_0.2_mps_after_s": settle(a, c, "speed", 0.2), "accel_within_0.5_mps2_after_s": settle(a, c, "accel", 0.5)}
                  for c in st.ACCEL_CHANGES[colour_by_athlete[a]]] for a in ATHLETE.values()}

    # no bridging: every sample's window (its last `window` seconds of used observations) has no internal gap > max_gap
    obs_by_athlete: dict[str, list[float]] = {}
    assigned = {x["track_id"]: x for x in assignments}
    for o in obs.observations:
        asg = assigned.get(o.track_id)
        if asg and o.frame >= asg["from_frame"]:
            obs_by_athlete.setdefault(asg["athlete_id"], []).append(o.video_time_s)
    bridged = 0
    for meta in log["samples"]:
        ts = [x for x in obs_by_athlete[meta["athlete_id"]] if meta["video_time_s"] - window - 1e-9 <= x <= meta["video_time_s"] + 1e-9]
        if any(b - a > max_gap + 1e-9 for a, b in zip(ts, ts[1:])):
            bridged += 1
    rate_ok = all(b["video_time_s"] - a["video_time_s"] >= 0.1 - 1e-6 for ath in ATHLETE.values()
                  for a, b in zip([m for m in log["samples"] if m["athlete_id"] == ath], [m for m in log["samples"] if m["athlete_id"] == ath][1:]))
    unassigned_tracks = {t: {**v, "colour": colour_of[t]} for t, v in log["unassigned_tracks"].items()}
    yellow_ids = {t for t, c in colour_of.items() if c == "yellow"}

    from camera_adapter.calibration import fit_homography  # noqa: PLC0415
    grid = [(u, v) for u in range(100, 900, 100) for v in range(220, 380, 40)]
    def m(h, u, v):
        w = h[2][0] * u + h[2][1] * v + h[2][2]
        return ((h[0][0] * u + h[0][1] * v + h[0][2]) / w, (h[1][0] * u + h[1][1] * v + h[1][2]) / w)
    def compare(img_pts, fld_pts):
        h_cv, _ = cv2.findHomography(np.array(img_pts, dtype=np.float64), np.array(fld_pts, dtype=np.float64), 0)
        ours = fit_homography(img_pts, fld_pts)
        return max(math.dist(m(ours, u, v), m(h_cv.tolist(), u, v)) for u, v in grid)
    exact_img = [list(camera.project(*mk)) for mk in FIT_MARKS]
    cv_diff_exact = compare(exact_img, [list(mk) for mk in FIT_MARKS])
    cv_diff = compare([p["image"] for p in points["fit_points"]], [p["field"] for p in points["fit_points"]])

    checks = {
        "no sample for an unassigned track (yellow, and blue before 1.5 s)": all(meta["track_id"] not in yellow_ids for meta in log["samples"])
            and all(not (meta["athlete_id"] == ATHLETE["blue"] and meta["video_time_s"] < 1.5) for meta in log["samples"]),
        "no identity swap (every sample nearest its own target)": swaps == 0,
        "no sample's window bridges a gap longer than max_gap_s": bridged == 0,
        "at most 10 samples per second per athlete": rate_ok,
        "adapter homography agrees with OpenCV findHomography on exact points (max difference over a grid < 1 mm)": cv_diff_exact < 1e-3,
    }
    report = {
        "tool": "camera_synthetic_check", "generated_at": datetime.now(UTC).isoformat(), "fictional_data": True, "extractor": obs.extractor,
        "video": {"width": st.WIDTH, "height": st.HEIGHT, "fps": st.FPS, "seconds": st.DURATION_S, "file": video.name},
        "calibration": {"fit_residual_rms_m": calib["fit_residual_m"]["rms"], "check_points_m": {k: calib["check_errors_m"][k] for k in ("rms", "max")}, "points_measured_as": "true image positions rounded to whole pixels",
                        "opencv_max_difference_m": {"exact_points": cv_diff_exact, "rounded_points": cv_diff,
                                                    "note": "both are least-squares fits but minimise different errors, so they differ when the points carry measurement error"}},
        "config": cfg, "time_mapping": log["time_mapping"],
        "observations": len(obs.observations), "samples": len(samples), "per_athlete": log["per_athlete"], "skipped": log["skipped"], "resets": log["resets"],
        "unassigned_tracks": unassigned_tracks,
        "errors": {a: {k: stats(v) for k, v in e.items()} for a, e in errors.items()},
        "settle_after_acceleration_changes": delays,
        "variant_window_1s": {"note": "same observations with window_s 1.0 and min_span_s 0.6: less noise, more lag near changes; not the default",
                              "samples": len(samples_1s), "identity_swaps": swaps_1s, "errors": {a: {k: stats(v) for k, v in e.items()} for a, e in errors_1s.items()},
                              "settle_after_acceleration_changes": {a: [{"change_s": c, "speed_within_0.2_mps_after_s": settle(a, c, "speed", 0.2, per_sample_1s), "accel_within_0.5_mps2_after_s": settle(a, c, "accel", 0.5, per_sample_1s)}
                                                                        for c in st.ACCEL_CHANGES[colour_by_athlete[a]]] for a in ATHLETE.values()}},
        "below_engine_distance_confidence_0.5": sum(1 for s in samples if s["tracking_confidence"] < 0.5),
        "identity_swaps": swaps, "bridged_windows": bridged, "checks": checks, "passed": all(checks.values()),
        "limitations": ["Synthetic markers drawn by this code and found by colour thresholds: not people, not real footage, no lens distortion, no real occlusion by bodies.",
                        "Errors are measurements of this setup with no acceptance threshold; they do not transfer to athletes or real cameras.",
                        "Acceleration reference is analytic; near changes of acceleration the estimator lags by about its window."],
    }
    return report


def main(argv: list[str] | None = None, *, out: Callable[[str], None] = print) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--workdir", type=Path, help="keep the generated video, observations, calibration and samples here (default: a temporary folder)")
    p.add_argument("--report", type=Path)
    args = p.parse_args(argv)
    try:
        import cv2  # noqa: F401, PLC0415
        import numpy  # noqa: F401, PLC0415
    except ImportError:
        out("This check needs OpenCV and NumPy: pip install -r requirements-vision.txt (the engine itself does not).")
        return 2
    if args.workdir:
        args.workdir.mkdir(parents=True, exist_ok=True)
        report = run(args.workdir, out)
    else:
        with tempfile.TemporaryDirectory(prefix="athletrix-camera-") as tmp:
            report = run(Path(tmp), out)
    path = args.report or ROOT / "reports" / f"camera-synthetic-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    out("SYNTHETIC CAMERA CHECK (synthetic marker tracking; fictional data)")
    out(f"  calibration: fit residual {report['calibration']['fit_residual_rms_m']:.4f} m rms (not independent); check points {report['calibration']['check_points_m']['rms']:.4f} m rms, {report['calibration']['check_points_m']['max']:.4f} m max")
    out(f"  {report['observations']} observations -> {report['samples']} samples; skipped {report['skipped']}; resets {len(report['resets'])}")
    for a, e in report["errors"].items():
        def f(k):
            s = e[k]
            return "n/a" if not s else f"median {s['median_abs']} p95 {s['p95_abs']} max {s['max_abs']} (n {s['n']})"
        out(f"  {a}: position m {f('position')}")
        out(f"    speed m/s steady {f('speed')}; near changes {f('speed_boundary')}; first window after a reset {f('speed_after_restart')}")
        out(f"    acceleration m/s2 steady {f('accel_steady')}; near changes {f('accel_boundary')}; first window after a reset {f('accel_after_restart')}")
    for a, e in report["variant_window_1s"]["errors"].items():
        out(f"  variant window 1.0 s, {a}: acceleration steady median {e['accel_steady']['median_abs']} p95 {e['accel_steady']['p95_abs']} max {e['accel_steady']['max_abs']}; near changes p95 {e['accel_boundary']['p95_abs'] if e['accel_boundary'] else 'n/a'}")
    out(f"  below the engine's 0.5 distance-confidence limit: {report['below_engine_distance_confidence_0.5']} samples; identity swaps {report['identity_swaps']}")
    for k, v in report["checks"].items():
        out(f"  {'PASS' if v else 'FAIL'}  {k}")
    out(f"JSON report: {path}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
