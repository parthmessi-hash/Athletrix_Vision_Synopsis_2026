"""Camera-only field check: measured points -> calibration -> independent check points -> course and timing intervals ->
quality report. Reads local files only; changes nothing else.

USAGE (the short workflow; every step is a file you can inspect)
    1. python scripts/camera_field_check.py template --out FIELD/
         writes FIELD/points.json (enter each measured mark: pixel position in the frame and field position in metres;
         keep at least 2 marks as CHECK points, never used in the fit; record the camera settings) and FIELD/course.json
         (the course line and its timing marks, in the same field metres).
    2. Track the clip and export samples (scripts/camera_track_video.py, then scripts/camera_to_samples.py export).
    3. python scripts/camera_field_check.py run --points FIELD/points.json --course FIELD/course.json \\
           --samples samples.jsonl --athlete ATHLETE_ID --out FIELD/report [--now-settings NOW.json --width W --height H]
         fits the calibration, prints each check point's error in metres, the usable field region (the area spanned by the
         measured marks; outside it positions are extrapolated), the share of the course that was tracked, every timing
         interval with its average speed or the reason it was excluded, and whether the calibration is still compatible
         with the camera as it is NOW (resolution, camera id, stated zoom/focus/mount).
    python scripts/camera_field_check.py synthetic --samples SAMPLES.jsonl --anchor-utc ISO --out DIR
         the same workflow on the SYNTHETIC geometry of camera_adapter/synthetic_people.py (samples from
         scripts/video_pilot_check.py, path A), compared with the analytic truth, plus a changed-zoom and a
         changed-resolution check that must be flagged.

WHAT IT REPORTS AND WHAT IT DOES NOT
  - Average interval speed = interval distance / time between the two mark crossings (each crossing interpolated between
    the two samples around it; its uncertainty is half their spacing). It is computed only when the samples around both
    marks are no further apart than the course's max_gap_s and no gap inside the interval exceeds it.
  - Peak speed is shown SEPARATELY as the camera's own estimate; its accuracy is NOT established: that needs an
    independent reference (timing gates or a measured video timing), which this check does not have.
  - Check-point errors are the only independent accuracy figure, and only at those points.

EXIT CODES: 0 report written (problems are reported, not errors) · 1 calibration incompatible with the camera now
(intervals are not computed) · 2 invalid input
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from camera_adapter import calibration as cal  # noqa: E402
from camera_adapter.setup import compatibility, inspect_calibration  # noqa: E402

COURSE_FORMAT = "athletrix-course"


def ts(s: dict) -> datetime:
    return datetime.fromisoformat(s["timestamp"].replace("Z", "+00:00"))


def template(out_dir: Path, out) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in ("points.json", "course.json"):
        if (out_dir / name).exists():
            out(f"REFUSED: {out_dir / name} exists"); return 2
    (out_dir / "points.json").write_text(json.dumps({
        "format": "athletrix-calibration-points", "format_version": 1, "camera_id": "sideline-cam-1",
        "image": {"width": 1920, "height": 1080},
        "camera_settings": {"camera_id": "sideline-cam-1", "zoom": "ENTER (e.g. wide end)", "focus": "ENTER (e.g. manual 10 m)", "mount": "ENTER (tripod, height, position)"},
        "fit_points": [{"label": "ENTER mark name", "image": [0, 0], "field": [0.0, 0.0]}],
        "check_points": [{"label": "ENTER a mark NOT used for fitting", "image": [0, 0], "field": [0.0, 0.0]}],
        "note": "at least 4 fit points (no 3 of any 4 in a line) and at least 2 check points, measured with a tape; field in metres"}, indent=1))
    (out_dir / "course.json").write_text(json.dumps({
        "format": COURSE_FORMAT, "format_version": 1, "axis": "x", "lane": {"y_min": 0.0, "y_max": 2.0},
        "marks": [{"label": "start", "at": 0.0}, {"label": "10 m", "at": 10.0}, {"label": "20 m", "at": 20.0}],
        "max_gap_s": 0.35, "min_tracking_confidence": 0.5,
        "note": "marks are positions along the axis in field metres, measured; lane bounds the other coordinate"}, indent=1))
    out(f"wrote {out_dir / 'points.json'} and {out_dir / 'course.json'}: fill them in from the tape measurements")
    return 0


def crossings(seq: list[tuple[datetime, float, dict]], at: float):
    """First crossing of position `at` along the axis: (time, half-spacing uncertainty, gap) or None."""
    for (t0, p0, _), (t1, p1, _) in zip(seq, seq[1:]):
        if (p0 - at) * (p1 - at) <= 0 and p0 != p1:
            f = (at - p0) / (p1 - p0)
            dt = (t1 - t0).total_seconds()
            return t0.timestamp() + f * dt, dt / 2, dt
    return None


def intervals(samples: list[dict], athlete: str, course: dict, region) -> dict:
    ax = 0 if course.get("axis", "x") == "x" else 1
    key = ("x_position", "y_position")
    lane = course.get("lane") or {}
    minc, gap_max = course.get("min_tracking_confidence", 0.5), course.get("max_gap_s", 0.35)
    mine = sorted((s for s in samples if s["athlete_id"] == athlete), key=ts)
    counts = {"samples": len(mine), "outside_usable_region": 0, "outside_lane": 0, "low_confidence_or_predicted": 0}
    seq = []
    x0, y0, x1, y1 = region
    for s in mine:
        x, y = s["x_position"], s["y_position"]
        other = s[key[1 - ax]]
        if not (x0 <= x <= x1 and y0 <= y <= y1):
            counts["outside_usable_region"] += 1
            continue
        if lane and not (lane.get("y_min", -1e9) <= other <= lane.get("y_max", 1e9)):
            counts["outside_lane"] += 1
            continue
        if s.get("tracking_confidence", 1) < minc or s.get("value_basis") == "predicted":
            counts["low_confidence_or_predicted"] += 1
            continue
        seq.append((ts(s), s[key[ax]], s))
    marks = course["marks"]
    rows, cross = [], {m["label"]: crossings(seq, m["at"]) for m in marks}
    for a, b in zip(marks, marks[1:]):
        ca, cb = cross[a["label"]], cross[b["label"]]
        row = {"interval": f"{a['label']} -> {b['label']}", "distance_m": round(abs(b["at"] - a["at"]), 3)}
        why = None
        if ca is None or cb is None:
            why = f"no tracked crossing of {a['label'] if ca is None else b['label']}"
        elif ca[2] > gap_max or cb[2] > gap_max:
            why = f"samples around {a['label'] if ca[2] > gap_max else b['label']} are {max(ca[2], cb[2]):.2f} s apart (limit {gap_max:g} s): crossing time not established"
        else:
            inside = [(t, s) for t, p, s in seq if ca[0] <= t.timestamp() <= cb[0]]
            gaps = [(inside[i][0], inside[i + 1][0]) for i in range(len(inside) - 1) if (inside[i + 1][0] - inside[i][0]).total_seconds() > gap_max]
            if gaps:
                g = max(gaps, key=lambda g: (g[1] - g[0]).total_seconds())
                why = f"tracking gap of {(g[1] - g[0]).total_seconds():.2f} s inside the interval (from {g[0].isoformat()[11:23]}): the athlete was not followed throughout"
            elif cb[0] <= ca[0]:
                why = "the finish crossing is not after the start crossing (wrong direction or a re-crossing)"
        if why:
            row.update(excluded=why)
        else:
            dt = cb[0] - ca[0]
            unc = ca[1] + cb[1]
            peak = max(s["speed_mps"] for t, s in inside)
            row.update(time_s=round(dt, 3), time_uncertainty_s=round(unc, 3), average_speed_mps=round(row["distance_m"] / dt, 3),
                       average_speed_range_mps=[round(row["distance_m"] / (dt + unc), 3), round(row["distance_m"] / max(dt - unc, 1e-6), 3)],
                       start_utc=datetime.fromtimestamp(ca[0], UTC).isoformat().replace("+00:00", "Z"),
                       peak_speed_mps_camera_estimate=round(peak, 3),
                       peak_speed_accuracy="NOT ESTABLISHED: needs an independent reference (gates or measured video timing)")
        rows.append(row)
    # course share tracked: from the first mark crossing to the last (or the tracked span inside the course)
    if seq:
        lo, hi = min(m["at"] for m in marks), max(m["at"] for m in marks)
        on = [(t.timestamp(), p) for t, p, _ in seq if lo <= p <= hi]
        covered = sum(min(b[0] - a[0], gap_max) for a, b in zip(on, on[1:]))
        span = (on[-1][0] - on[0][0]) if len(on) > 1 else 0.0
        share = {"tracked_s": round(covered, 3), "time_on_course_s": round(span, 3), "share": round(covered / span, 3) if span > 0 else None,
                 "marks_crossed": [k for k, v in cross.items() if v is not None]}
    else:
        share = {"tracked_s": 0.0, "time_on_course_s": None, "share": None, "marks_crossed": []}
    return {"counts": counts, "intervals": rows, "course_tracked": share}


def fit_points(pts: dict, out):
    try:
        return cal.fit(pts["image"]["width"], pts["image"]["height"], pts["fit_points"], pts.get("check_points"), camera_id=pts.get("camera_id", ""),
                       camera_settings=pts.get("camera_settings"))
    except (cal.CalibrationError, KeyError, TypeError) as exc:
        out("CALIBRATION NOT FITTED: " + "; ".join(getattr(exc, "errors", [str(exc)])))
        return None


def fit_cmd(args, out) -> int:
    """Step 2 of the bench check: fit the calibration from the measured points (no report needed first)."""
    if args.out.exists():
        out(f"REFUSED: {args.out} exists"); return 2
    try:
        pts = json.loads(args.points.read_text())
    except (OSError, ValueError) as exc:
        out(f"INVALID INPUT: {exc}"); return 2
    if "ENTER" in json.dumps(pts):
        out(f"REFUSED: {args.points} still contains ENTER placeholders: fill in every measured value first"); return 2
    c = fit_points(pts, out)
    if c is None:
        return 2
    c.save(args.out)
    out(f"wrote {args.out}: fit residual RMS {c.fit_residual_m['rms']:.3f} m (not accuracy); check points " +
        (", ".join(f"{p['label']} {p['error_m']:.3f} m" for p in c.check_errors_m["per_point"]) if c.check_errors_m else "NONE (accuracy unknown)"))
    return 0


def bench(args, out) -> int:
    """A camera bench check with NO participant and NO wearable: three separate answers.
      source     can the local clip be read, at the calibrated resolution (a camera is never opened by this tool)
      geometry   do the separately measured check points agree with the calibration, and is the camera unchanged
      tracking   was the person detector and tracker exercised at all; a board or an empty field cannot test it"""
    d = args.out
    d.mkdir(parents=True, exist_ok=True)
    try:
        pts = json.loads(args.points.read_text())
    except (OSError, ValueError) as exc:
        out(f"INVALID INPUT: {exc}"); return 2
    if args.calibration:
        c = cal.load(args.calibration)
        cpath = args.calibration
    else:
        c = fit_points(pts, out)
        if c is None:
            return 2
        cpath = d / "calibration.json"
        c.save(cpath)
    res: dict = {"label": args.label or "bench check", "calibration": str(cpath)}
    # 1. source
    if not args.clip:
        res["source"] = {"verdict": "NOT CHECKED", "why": "no local clip given (this tool never opens a camera)"}
    else:
        try:
            import cv2  # noqa: PLC0415
            cap = cv2.VideoCapture(str(args.clip))
            ok, _ = cap.read()
            n, fps = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)), cap.get(cv2.CAP_PROP_FPS)
            w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            cap.release()
        except ImportError:
            ok, n, fps, w, h = None, None, None, None, None
        if ok is None:
            res["source"] = {"verdict": "NOT CHECKED", "why": "OpenCV is not installed (requirements-video.txt); geometry below does not need it"}
        elif not ok:
            res["source"] = {"verdict": "FAIL", "why": f"{args.clip} could not be decoded"}
        else:
            same = [w, h] == [c.width, c.height]
            res["source"] = {"verdict": "PASS" if same else "FAIL", "frames": n, "fps": round(fps, 3), "resolution": [w, h],
                             "why": None if same else f"clip is {w}x{h}, the calibration is {c.width}x{c.height}"}
    # 2. geometry
    now = json.loads(args.now_settings.read_text()) if args.now_settings else None
    comp = compatibility(inspect_calibration(Path(cpath)), {"width": pts["image"]["width"], "height": pts["image"]["height"],
                                                             "camera_id": (now or {}).get("camera_id"), "camera_settings": now})
    blocks = [x["text"] for x in comp if x["level"] == "block"]
    per = (c.check_errors_m or {}).get("per_point", [])
    too_far = [p for p in per if args.max_check_error_m is not None and p["error_m"] > args.max_check_error_m]
    if not per:
        g = "FAIL"
        why = ["no check points: geometry cannot be checked"]
    elif blocks or too_far:
        g = "FAIL"
        why = blocks + [f"check point {p['label']}: {p['error_m']:.3f} m (limit {args.max_check_error_m:g} m): re-measure it or the marks used for fitting" for p in too_far]
    elif args.max_check_error_m is None:
        g, why = "REPORTED", ["no tolerance stated (--max-check-error-m): the team decides what error is acceptable"]
    else:
        g, why = "PASS", []
    x0, y0, x1, y1 = c.field_bounds()
    res["geometry"] = {"verdict": g, "why": why, "check_points_m": [{"label": p["label"], "error_m": round(p["error_m"], 3)} for p in per],
                       "fit_residual_rms_m": round(c.fit_residual_m["rms"], 3), "usable_region_m": {"x": [x0, x1], "y": [y0, y1]},
                       "compatibility_now": comp, "tolerance_m": args.max_check_error_m}
    # 3. person detection and tracking
    if not args.clip or not args.model:
        res["tracking"] = {"verdict": "NOT EXERCISED", "why": "no clip and model given: nothing about person detection or tracking was tested"}
    elif not Path(args.model).is_file():
        res["tracking"] = {"verdict": "NOT EXERCISED", "why": f"model {args.model} is not on this laptop (it is never downloaded)"}
    else:
        import subprocess  # noqa: PLC0415
        cmd = [sys.executable, str(ROOT / "scripts" / "camera_track_video.py"), str(args.clip), "--model", str(args.model), "--out", str(d / "obs.jsonl"),
               "--log", str(d / "track.json")] + (["--config", str(args.config)] if args.config else []) + (["--max-frames", str(args.max_frames)] if args.max_frames else [])
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
        if r.returncode != 0:
            res["tracking"] = {"verdict": "FAIL", "why": "tracking did not run: " + (r.stdout + r.stderr).strip().splitlines()[-1][:300]}
        else:
            log = json.loads((d / "track.json").read_text())
            n_tracks = len(log.get("tracks") or {})
            res["tracking"] = {"verdict": "EXERCISED" if n_tracks else "EXERCISED, NO PERSON FOUND", "tracks": n_tracks, "detections": log.get("detections"),
                               "frames_per_second": (log.get("speed") or {}).get("frames_per_second"),
                               "why": ("person tracks were produced; identities and positional accuracy are NOT checked here" if n_tracks else
                                       "the detector ran but saw nobody: a board or an empty field says nothing about player tracking")}
    (d / "bench.json").write_text(json.dumps(res, indent=1, default=str))
    L = [f"# Camera bench check ({res['label']})", "", "Three separate answers; none of them is a person-tracking or field-accuracy result unless it says so.", ""]
    for k, title in (("source", "1. Source readable"), ("geometry", "2. Calibration geometry"), ("tracking", "3. Person detection and tracking")):
        x = res[k]
        L.append(f"**{title}: {x['verdict']}**" + (f" - {'; '.join(x['why'])}" if isinstance(x.get("why"), list) and x["why"] else (f" - {x['why']}" if isinstance(x.get("why"), str) else "")))
        if k == "geometry":
            L += [f"- check point {p['label']}: {p['error_m']} m" for p in x["check_points_m"]] + [f"- {y['level'].upper()}: {y['text']}" for y in x["compatibility_now"]]
        L.append("")
    (d / "bench.md").write_text("\n".join(L))
    for line in L:
        out(line)
    return 1 if "FAIL" in (res["source"]["verdict"], res["geometry"]["verdict"], res["tracking"]["verdict"]) else 0


def bench_example(args, out) -> int:
    """The bench check on SYNTHETIC geometry: a correct measurement, one intentionally wrong check point, and a zoom
    change after calibration, so the operator sees what a failed check looks like. Optional: an EMPTY generated clip
    (field colour only) to show that a detector that sees nobody tells nothing about player tracking."""
    from camera_adapter import synthetic_people as sp  # noqa: PLC0415
    from camera_adapter.evaluation import PEOPLE_CHECK_MARKS, PEOPLE_FIT_MARKS  # noqa: PLC0415
    d = args.out
    d.mkdir(parents=True, exist_ok=True)
    cam = sp.Camera3()
    mark = lambda x, y, dx=0.0: {"label": f"mark ({x}, {y})", "image": [round(v) for v in cam.project(x + dx, y)], "field": [x, y]}  # noqa: E731
    settings = {"camera_id": "synthetic-people-camera", "zoom": "wide end", "focus": "manual 12 m", "mount": "tripod A, 4 m behind the near line"}
    base = {"format": "athletrix-calibration-points", "format_version": 1, "camera_id": "synthetic-people-camera", "image": {"width": sp.WIDTH, "height": sp.HEIGHT},
            "camera_settings": settings, "fit_points": [mark(*m) for m in PEOPLE_FIT_MARKS]}
    (d / "points-good.json").write_text(json.dumps({**base, "check_points": [mark(*m) for m in PEOPLE_CHECK_MARKS]}, indent=1))
    bad = [mark(*m) for m in PEOPLE_CHECK_MARKS]
    bad[1] = mark(*PEOPLE_CHECK_MARKS[1], dx=0.8)   # the pixel was read 0.8 m off along x: an intentionally wrong check point
    bad[1]["label"] += " (INTENTIONALLY WRONG by 0.8 m)"
    (d / "points-wrong-check.json").write_text(json.dumps({**base, "check_points": bad}, indent=1))
    (d / "now-same.json").write_text(json.dumps(settings))
    (d / "now-zoomed.json").write_text(json.dumps({**settings, "zoom": "2x"}))
    clip_args = []
    if args.with_clip:
        try:
            import cv2  # noqa: PLC0415
            import numpy as np  # noqa: PLC0415
            wr = cv2.VideoWriter(str(d / "empty-field.avi"), cv2.VideoWriter_fourcc(*"MJPG"), 25.0, (sp.WIDTH, sp.HEIGHT))
            for _ in range(50):
                wr.write(np.full((sp.HEIGHT, sp.WIDTH, 3), (55, 115, 55), np.uint8))
            wr.release()
            clip_args = ["--clip", str(d / "empty-field.avi")] + (["--model", str(args.model), "--config", str(ROOT / "scripts/examples/camera/video_tracking_config_tiny.json")]
                                                                  if args.model else [])
        except ImportError:
            out("OpenCV not installed: the clip part is skipped; geometry does not need it")
    res = {}
    for tag, pts, now in (("good", "points-good.json", "now-same.json"), ("wrong-check-point", "points-wrong-check.json", "now-same.json"),
                          ("zoom-changed", "points-good.json", "now-zoomed.json")):
        lines: list = []
        code = main(["bench", "--points", str(d / pts), "--now-settings", str(d / now), "--max-check-error-m", str(args.max_check_error_m), "--out", str(d / tag),
                     "--label", f"SYNTHETIC example: {tag}"] + clip_args, out=lines.append)
        res[tag] = {"exit": code, **{k: json.loads((d / tag / "bench.json").read_text())[k]["verdict"] for k in ("source", "geometry", "tracking")}}
        out(f"{tag}: exit {code} · source {res[tag]['source']} · geometry {res[tag]['geometry']} · tracking {res[tag]['tracking']}")
    (d / "examples.json").write_text(json.dumps(res, indent=1))
    ok = res["good"]["geometry"] == "PASS" and res["wrong-check-point"]["geometry"] == "FAIL" and res["zoom-changed"]["geometry"] == "FAIL"
    out("PASS: the correct example passes, the wrong check point and the zoom change fail" if ok else "UNEXPECTED: see examples.json")
    return 0 if ok else 1


def run(args, out) -> int:
    try:
        pts = json.loads(args.points.read_text())
        course = json.loads(args.course.read_text())
        if course.get("format") != COURSE_FORMAT or len(course.get("marks", [])) < 2:
            raise ValueError("course.json must be an athletrix-course with at least two marks")
        samples = [json.loads(x)["sample"] for x in args.samples.read_text().splitlines() if x.strip()]
    except (OSError, ValueError, KeyError) as exc:
        out(f"INVALID INPUT: {exc}"); return 2
    args.out.mkdir(parents=True, exist_ok=True)
    if getattr(args, "calibration", None):
        try:
            c = cal.load(args.calibration)
        except (cal.CalibrationError, OSError, ValueError) as exc:
            out("CALIBRATION NOT READ: " + "; ".join(getattr(exc, "errors", [str(exc)]))); return 2
        if [p["field"] for p in c.fit_points] != [p["field"] for p in pts["fit_points"]] or [p["image"] for p in c.fit_points] != [p["image"] for p in pts["fit_points"]]:
            out(f"REFUSED: {args.calibration} was not fitted from {args.points} (different fit points); fit it again with the fit command"); return 2
        cpath = args.calibration
    else:
        c = fit_points(pts, out)
        if c is None:
            return 2
        cpath = args.out / "calibration.json"
        c.save(cpath)
    summ = inspect_calibration(cpath)
    now_settings = json.loads(args.now_settings.read_text()) if args.now_settings else None
    target = {"width": args.width or pts["image"]["width"], "height": args.height or pts["image"]["height"],
              "camera_id": (now_settings or {}).get("camera_id"), "camera_settings": now_settings}
    comp = compatibility(summ, target)
    blocked = [x["text"] for x in comp if x["level"] == "block"]
    region = c.field_bounds()
    rep = {"calibration": {"file": cpath.name, "sha256": summ.get("sha256"), "fit_residual_m": summ.get("fit_residual_m"),
                           "check_points_m": [{"label": p["label"], "error_m": round(p["error_m"], 3)} for p in (c.check_errors_m or {}).get("per_point", [])],
                           "check_rms_m": round(c.check_errors_m["rms"], 3) if c.check_errors_m else None,
                           "check_note": "the only independent accuracy figure, and only at these points" if c.check_errors_m else "NO check points: accuracy unknown"},
           "usable_field_region_m": {"x": [region[0], region[2]], "y": [region[1], region[3]],
                                     "meaning": "the rectangle spanned by the measured marks; positions outside it are extrapolated and are left out here"},
           "compatibility_now": comp, "calibration_usable_now": not blocked}
    if blocked:
        rep["intervals"] = None
        rep["why_no_intervals"] = "calibration STALE or INCOMPATIBLE with the camera as it is now: " + "; ".join(blocked) + ". Recalibrate before timing."
    else:
        rep.update(intervals(samples, args.athlete, course, region))
    (args.out / "report.json").write_text(json.dumps(rep, indent=1, default=str))
    L = ["# Camera-only field check", "", f"Calibration {cpath.name} ({(summ.get('sha256') or '')[:12]}): fit residual RMS {summ['fit_residual_m']['rms']} m (not accuracy)."]
    L += ["", "## Check points (independent)", ""] + ([f"- {x['label']}: {x['error_m']} m" for x in rep["calibration"]["check_points_m"]] or ["- none: accuracy unknown"])
    L += ["", f"Usable field region: x {region[0]}..{region[2]} m, y {region[1]}..{region[3]} m (outside it: extrapolation, left out).", "", "## Compatibility with the camera now", ""]
    L += [f"- {x['level'].upper()}: {x['text']}" for x in comp]
    if blocked:
        L += ["", "**" + rep["why_no_intervals"] + "**"]
    else:
        ct = rep["course_tracked"]
        L += ["", "## Course", "", f"Tracked {ct['tracked_s']} s of {ct['time_on_course_s']} s on the course (share {ct['share']}); marks crossed: {', '.join(ct['marks_crossed']) or 'none'}.",
              f"Samples left out: {json.dumps(rep['counts'])}.", "", "| Interval | distance (m) | time (s) | average speed (m/s) | peak speed (camera estimate) | excluded because |", "|---|---|---|---|---|---|"]
        for r in rep["intervals"]:
            L.append(f"| {r['interval']} | {r['distance_m']} | {str(r['time_s']) + ' ± ' + str(r['time_uncertainty_s']) if 'time_s' in r else '-'} | "
                     f"{r.get('average_speed_mps', '-')} | {str(r['peak_speed_mps_camera_estimate']) + ' (accuracy not established)' if 'time_s' in r else '-'} | {r.get('excluded', '')} |")
        L += ["", "Average interval speed and peak speed are separate quantities. Peak-speed accuracy needs an independent reference; none was used here."]
    (args.out / "report.md").write_text("\n".join(L) + "\n")
    for line in L:
        out(line)
    return 1 if blocked else 0


def synthetic(args, out) -> int:
    """The workflow on the synthetic geometry, compared with the analytic truth; then the stale/incompatible checks."""
    from camera_adapter import synthetic_people as sp  # noqa: PLC0415
    from camera_adapter.evaluation import PEOPLE_CHECK_MARKS, PEOPLE_FIT_MARKS  # noqa: PLC0415
    d = args.out
    d.mkdir(parents=True, exist_ok=True)
    cam = sp.Camera3()
    mark = lambda x, y: {"label": f"mark ({x}, {y})", "image": [round(v) for v in cam.project(x, y)], "field": [x, y]}  # noqa: E731
    settings = {"camera_id": "synthetic-people-camera", "zoom": "fixed (synthetic)", "focus": "n/a", "mount": "virtual, 6 m behind the near sideline"}
    (d / "points.json").write_text(json.dumps({"format": "athletrix-calibration-points", "format_version": 1, "camera_id": "synthetic-people-camera",
                                               "image": {"width": sp.WIDTH, "height": sp.HEIGHT}, "camera_settings": settings,
                                               "fit_points": [mark(*m) for m in PEOPLE_FIT_MARKS], "check_points": [mark(*m) for m in PEOPLE_CHECK_MARKS]}, indent=1))
    (d / "course.json").write_text(json.dumps({"format": COURSE_FORMAT, "format_version": 1, "axis": "x", "lane": {"y_min": 2.0, "y_max": 4.0},
                                               "marks": [{"label": "6 m", "at": 6.0}, {"label": "12 m", "at": 12.0}, {"label": "18 m", "at": 18.0}],
                                               "max_gap_s": 0.35, "min_tracking_confidence": 0.5, "note": "SYNTHETIC: red's straight run along y = 3 m"}, indent=1))
    (d / "now-same.json").write_text(json.dumps(settings))
    (d / "now-zoomed.json").write_text(json.dumps({**settings, "zoom": "2x (changed after calibration)"}))
    res = {}
    for tag, extra in (("same", ["--now-settings", str(d / "now-same.json")]), ("zoom-changed", ["--now-settings", str(d / "now-zoomed.json")]),
                       ("resolution-changed", ["--now-settings", str(d / "now-same.json"), "--width", "1920", "--height", "1080"])):
        lines: list = []
        code = main(["run", "--points", str(d / "points.json"), "--course", str(d / "course.json"), "--samples", str(args.samples), "--athlete", args.athlete,
                     "--out", str(d / f"report-{tag}"), *extra], out=lines.append)
        res[tag] = {"exit": code, "report": json.loads((d / f"report-{tag}" / "report.json").read_text())}
    # truth: crossing times of red along y = 3
    anchor = datetime.fromisoformat(args.anchor_utc.replace("Z", "+00:00"))

    def cross(at):
        t = 0.0
        while t < sp.DURATION_S:
            if sp.truth("red", t)[0][0] >= at:
                return t
            t += 0.0005
        return None
    truth = {}
    for a, b in ((6.0, 12.0), (12.0, 18.0)):
        ta, tb = cross(a), cross(b)
        truth[f"{a:g} m -> {b:g} m"] = {"time_s": round(tb - ta, 4), "average_speed_mps": round((b - a) / (tb - ta), 4), "start_video_s": round(ta, 4),
                                        "peak_speed_mps": round(max(sp.truth("red", ta + k * (tb - ta) / 200)[3] for k in range(201)), 4)}
    cmp_ = []
    for r in res["same"]["report"]["intervals"]:
        tr = truth.get(r["interval"])
        row = {"interval": r["interval"], "truth": tr}
        if "time_s" in r:
            row.update(measured_time_s=r["time_s"], time_error_s=round(r["time_s"] - tr["time_s"], 4), measured_average_speed_mps=r["average_speed_mps"],
                       average_speed_error_mps=round(r["average_speed_mps"] - tr["average_speed_mps"], 4),
                       peak_camera_estimate=r["peak_speed_mps_camera_estimate"], peak_truth_synthetic_only=tr["peak_speed_mps"],
                       start_error_s=round((datetime.fromisoformat(r["start_utc"].replace("Z", "+00:00")) - anchor).total_seconds() - tr["start_video_s"], 4))
        else:
            row["excluded"] = r["excluded"]
        cmp_.append(row)
    summary = {"label": "SYNTHETIC geometry (camera_adapter/synthetic_people.py); samples from the actual video path on a generated clip; truth is analytic",
               "runs": {k: {"exit": v["exit"], "calibration_usable_now": v["report"]["calibration_usable_now"],
                            "blocks": [x["text"] for x in v["report"]["compatibility_now"] if x["level"] == "block"]} for k, v in res.items()},
               "check_points_m": res["same"]["report"]["calibration"]["check_points_m"], "usable_region": res["same"]["report"]["usable_field_region_m"],
               "course_tracked": res["same"]["report"]["course_tracked"], "intervals_vs_truth": cmp_,
               "note": "the synthetic truth gives a peak-speed comparison here only; on the field there is no such truth without an independent reference"}
    (d / "synthetic-summary.json").write_text(json.dumps(summary, indent=1))
    out(json.dumps(summary, indent=1))
    ok = res["same"]["exit"] == 0 and res["zoom-changed"]["exit"] == 1 and res["resolution-changed"]["exit"] == 1
    out("PASS: compatible run measured; zoom and resolution changes flagged" if ok else "FAIL: see synthetic-summary.json")
    return 0 if ok else 1


def main(argv=None, out=print) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("template"); t.add_argument("--out", type=Path, required=True)
    r = sub.add_parser("run")
    for k in ("points", "course", "samples", "out"):
        r.add_argument(f"--{k}", type=Path, required=True)
    r.add_argument("--athlete", required=True); r.add_argument("--now-settings", type=Path); r.add_argument("--width", type=int); r.add_argument("--height", type=int)
    r.add_argument("--calibration", type=Path, help="use this fitted calibration (from the fit command) instead of fitting again")
    f = sub.add_parser("fit"); f.add_argument("--points", type=Path, required=True); f.add_argument("--out", type=Path, required=True)
    b = sub.add_parser("bench")
    b.add_argument("--points", type=Path, required=True); b.add_argument("--out", type=Path, required=True); b.add_argument("--calibration", type=Path)
    b.add_argument("--now-settings", type=Path); b.add_argument("--max-check-error-m", type=float); b.add_argument("--clip", type=Path)
    b.add_argument("--model", type=Path); b.add_argument("--config", type=Path); b.add_argument("--max-frames", type=int); b.add_argument("--label")
    e = sub.add_parser("bench-example"); e.add_argument("--out", type=Path, required=True); e.add_argument("--max-check-error-m", type=float, default=0.25)
    e.add_argument("--with-clip", action="store_true"); e.add_argument("--model", type=Path)
    s = sub.add_parser("synthetic"); s.add_argument("--samples", type=Path, required=True); s.add_argument("--anchor-utc", required=True)
    s.add_argument("--out", type=Path, required=True); s.add_argument("--athlete", default="vx1")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    if args.cmd == "template":
        return template(args.out, out)
    if args.cmd == "run":
        return run(args, out)
    if args.cmd == "fit":
        return fit_cmd(args, out)
    if args.cmd == "bench":
        return bench(args, out)
    if args.cmd == "bench-example":
        return bench_example(args, out)
    return synthetic(args, out)


if __name__ == "__main__":
    sys.exit(main())
