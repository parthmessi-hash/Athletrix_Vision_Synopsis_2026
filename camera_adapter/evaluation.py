"""Shared synthetic evaluation for the camera adapter (used by scripts/camera_synthetic_check.py).

  people     the real person detector + ByteTrack on the drawn-figure scene (camera_adapter/synthetic_people.py):
             detection coverage, track fragmentation, identity mismatches, ground-point error behind a board, and the
             adapter's position and speed errors against the analytic truth. Needs OpenCV, NumPy, trackers and the
             fetched model file.
  estimator  the causal estimator alone over analytic scenarios (stationary, constant speed, acceleration and
             deceleration, turns, near and far perspective, pixel noise, partial-occlusion bias, longer gaps, track
             changes) for a small set of configurations: errors, unavailable time, response lag, distance coverage and
             error through the engine's own distance rule, and throughput. Plain Python.

Every reference value is analytic (closed-form paths and a pinhole projection), never produced by the code under test.
Development and held-out scenario variants use different seeds and geometry; a configuration is never chosen on the
same examples it is reported on. No acceptance threshold is applied to measured errors: they describe this synthetic
setup only.
"""

from __future__ import annotations

import math
import random
import statistics
import time
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta

from camera_adapter import calibration as cal
from camera_adapter.estimator import EstimatorConfig
from camera_adapter.observations import Assignment, Observation, ObservationFile, Video, apply_assignments
from camera_adapter.pipeline import AdapterConfig, TimeMapping, process, records


def stats(values: list[float]) -> dict | None:
    v = sorted(abs(x) for x in values)
    if not v:
        return None
    return {"n": len(v), "mean_abs": round(statistics.fmean(v), 4), "median_abs": round(v[len(v) // 2], 4),
            "p95_abs": round(v[min(len(v) - 1, math.ceil(0.95 * len(v)) - 1)], 4), "max_abs": round(v[-1], 4)}


def engine_distance(samples: list[dict], t0: datetime, start_s: float, end_s: float) -> dict:
    """The engine's own distance rule (app/core/distance.py, default settings) over [start_s, end_s] of video time."""
    from app.core.distance import estimate_distance  # noqa: PLC0415
    from app.models import TrackingSample  # noqa: PLC0415
    sel = [TrackingSample.model_validate(s) for s in samples]
    sel = [s for s in sel if t0 + timedelta(seconds=start_s) <= s.timestamp <= t0 + timedelta(seconds=end_s)]
    d = estimate_distance(sel, end_s - start_s)
    return {"estimated_distance_m": None if d.estimated_distance_m is None else round(d.estimated_distance_m, 3), "included_duration_s": round(d.included_duration_seconds, 3),
            "included_fraction": round(d.included_window_fraction, 4), "excluded_intervals": d.excluded_interval_count, "continuity_breaks": d.continuity_break_count, "samples": len(sel)}


# ==== people: real detector on drawn figures ===============================================================================

PEOPLE_ATHLETES = {"red": "fictional-athlete-01", "blue": "fictional-athlete-02", "yellow": "fictional-athlete-03", "white": "fictional-athlete-04"}   # black is never assigned
PEOPLE_FIT_MARKS = [(4, 0), (12, 0), (20, 0), (0, 14), (12, 14), (24, 14)]
PEOPLE_CHECK_MARKS = [(12, 7), (6, 10), (18, 4), (21, 12)]
T0 = datetime(2026, 10, 20, 10, 0, 0, tzinfo=UTC)


def people_check(workdir, model_path, out) -> dict:
    """Render the drawn-figure scene, run the real tracker, assign as an operator would, run the adapter, compare with truth."""
    import json  # noqa: PLC0415

    from camera_adapter import person_video as pv  # noqa: PLC0415
    from camera_adapter import synthetic_people as sp  # noqa: PLC0415
    camera = sp.Camera3()
    video = workdir / "synthetic-people.avi"
    sp.render(video, camera)
    config = pv.VideoTrackingConfig()
    model_info = pv.verify_model(model_path, config.detector.model)
    detector = pv.YoloxPersonDetector(model_path, config.detector)
    preview = pv.PreviewWriter(None, workdir / "synthetic-people-tracks.png", sp.FPS, (sp.WIDTH, sp.HEIGHT))
    obs, log = pv.track_video(video, config, detector=detector, model_info=model_info, on_frame=preview)
    preview.close()

    def mark(x, y):
        u, v = camera.project(x, y)
        return {"label": f"mark ({x}, {y})", "image": [round(u), round(v)], "field": [x, y]}
    calib = cal.fit(sp.WIDTH, sp.HEIGHT, [mark(*m) for m in PEOPLE_FIT_MARKS], [mark(*m) for m in PEOPLE_CHECK_MARKS], camera_id="synthetic-people-camera")

    def foot_px(name, t):
        st = sp.truth(name, t)
        return None if st is None else camera.project(*st[0])

    def height_px(name, t):
        (x, y), *_ = sp.truth(name, t)
        return camera.project3(x, y, 0)[1] - camera.project3(x, y, sp.FIGURE_HEIGHT_M)[1]

    def nearest(o):
        best = None
        for name in sp.FIGURES:
            p = foot_px(name, o.video_time_s)
            if p is not None:
                d = math.dist((o.u_px, o.v_px), p)
                if best is None or d < best[1]:
                    best = (name, d)
        return best

    # the operator's step, simulated: confirm each track from its first observations (as from the gallery); black stays unassigned
    first: dict[str, Observation] = {}
    for o in obs.observations:
        first.setdefault(o.track_id, o)
    assignments, track_figure = [], {}
    for tid, o in sorted(first.items()):
        name, d = nearest(o)
        track_figure[tid] = name
        if name in PEOPLE_ATHLETES and d <= max(15.0, 0.25 * height_px(name, o.video_time_s)):
            assignments.append(Assignment(tid, PEOPLE_ATHLETES[name], o.frame, None, "operator step simulated from the first observation"))
    assigned = apply_assignments(obs, assignments)
    # identity audit: an assigned observation whose box foot is nearer another figure than its own (track drifted or merged)
    mismatches: dict[str, int] = {}
    athlete_figure = {v: k for k, v in PEOPLE_ATHLETES.items()}
    for o in assigned.observations:
        if o.athlete_id:
            name, _ = nearest(o)
            if name != athlete_figure[o.athlete_id]:
                mismatches[o.athlete_id] = mismatches.get(o.athlete_id, 0) + 1
    # detection coverage: frames where a figure's feet are in view and some tracked observation lies near them
    by_frame: dict[int, list[Observation]] = {}
    for o in obs.observations:
        by_frame.setdefault(o.frame, []).append(o)
    coverage, hidden_err = {}, []
    n_frames = int(round(sp.DURATION_S * sp.FPS))
    for name in sp.FIGURES:
        seen = total = 0
        for f in range(n_frames):
            t = f / sp.FPS
            if not sp.in_view(camera, name, t, 20):
                continue
            total += 1
            p, tol = foot_px(name, t), max(15.0, 0.25 * height_px(name, t))
            hits = [o for o in by_frame.get(f, []) if math.dist((o.u_px, o.v_px), p) <= tol]
            if hits:
                seen += 1
            if name == "yellow" and sp.legs_hidden(name, t):
                near = [o for o in by_frame.get(f, []) if abs(o.u_px - p[0]) < 0.5 * height_px(name, t)]
                if near:
                    o = min(near, key=lambda o: abs(o.u_px - p[0]))
                    pos = calib.to_field(o.u_px, o.v_px)
                    if pos:
                        hidden_err.append(math.dist(pos, sp.truth(name, t)[0]))
        coverage[name] = {"frames_in_view": total, "frames_detected_near_feet": seen, "fraction": round(seen / total, 4) if total else None}

    acfg = AdapterConfig(device_id="synthetic-people-cam-1")
    res = process(assigned, calib, acfg)
    mapping = TimeMapping(T0, 0.0, "fixed anchor for the synthetic check; not capture time")
    samples = [r["sample"] for r in records(res, mapping, acfg, "people")]
    errors = {a: {"position": [], "position_legs_hidden": [], "speed": [], "speed_near_changes": []} for a in PEOPLE_ATHLETES.values()}
    window = acfg.estimator.window_s
    for out_, s in zip(res.outputs, samples):
        name = athlete_figure[s["athlete_id"]]
        pos, _, _, speed = sp.truth(name, out_.video_time_s)
        e = math.dist((s["x_position"], s["y_position"]), pos)
        hidden = name == "yellow" and any(sp.legs_hidden(name, out_.video_time_s - k / sp.FPS) for k in range(int(window * sp.FPS) + 1))
        errors[s["athlete_id"]]["position_legs_hidden" if hidden else "position"].append(e)
        near = any(out_.video_time_s - window - 1e-9 <= c <= out_.video_time_s + 1e-9 for c in sp.SPEED_CHANGES[name])
        errors[s["athlete_id"]]["speed_near_changes" if near or hidden else "speed"].append(s["speed_mps"] - speed)
    red = PEOPLE_ATHLETES["red"]
    red_run = engine_distance([s for s in samples if s["athlete_id"] == red], T0, 2.0, 8.0)
    report = {
        "scene": {"video": video.name, "frames": n_frames, "fps": sp.FPS, "size": [sp.WIDTH, sp.HEIGHT], "figures": list(sp.FIGURES),
                  "note": "cartoon figures drawn by camera_adapter/synthetic_people.py; no footage of any person"},
        "model": model_info, "tracking_config": config.to_doc(), "tracking_log": {k: log[k] for k in ("detections", "observations", "skipped", "tracks", "track_losses", "speed")},
        "calibration": {"fit_residual_rms_m": calib.fit_residual_m["rms"], "check_points_m": {k: calib.check_errors_m[k] for k in ("rms", "max")}},
        "track_to_figure_at_first_observation": track_figure,
        "assignments": [asdict(a) for a in assignments], "unassigned_tracks": sorted(t for t in first if t not in {a.track_id for a in assignments}),
        "tracks_per_figure": {n: sorted(t for t, f in track_figure.items() if f == n) for n in sp.FIGURES},
        "identity_mismatch_observations": mismatches,
        "detection_coverage": coverage,
        "ground_point_error_while_legs_hidden_m": stats(hidden_err),
        "adapter": {"samples": len(samples), "skipped": res.skipped, "resets": len(res.resets), "segments": len(res.segments)},
        "errors": {a: {k: stats(v) for k, v in e.items()} for a, e in errors.items()},
        "engine_distance_red_2_to_8_s": {**red_run, "truth_m": 16.0},
        "limitations": ["Drawn cartoon figures on a clean striped field: real kits, motion blur, crowds, shadows, lighting, camera shake and compression are absent.",
                        "The operator's assignment step is simulated from the analytic truth at each track's first observation.",
                        "One fixed camera, no lens distortion, no camera movement; detector behaviour on real footage is not shown by this check."],
    }
    out(f"  people scene: {log['detections']} detections, {len(log['tracks'])} tracks, {log['observations']} observations at {log['speed']['frames_per_second']} frames/s")
    return report


# ==== estimator: configuration comparison on analytic scenarios ============================================================

FPS_EST = 25.0


class Path2:
    """A continuous analytic path built from rest, straight and circular-arc pieces; truth(t) -> (pos, speed, tangential accel)."""

    def __init__(self, start, heading_deg, pieces):
        self.pieces = []
        t, pos, hd, v = 0.0, start, math.radians(heading_deg), 0.0
        for p in pieces:
            kind, dur = p[0], p[1]
            if kind == "rest":
                self.pieces.append((t, t + dur, "rest", pos, hd, 0.0, 0.0, None))
                v = 0.0
            else:
                a = p[2]
                radius = p[3] if kind == "arc" else None
                self.pieces.append((t, t + dur, kind, pos, hd, v, a, radius))
                (pos, hd, v) = self._advance(kind, pos, hd, v, a, radius, dur)
            t += dur
        self.duration = t
        self.changes = [pc[0] for pc in self.pieces[1:]]

    @staticmethod
    def _advance(kind, pos, hd, v, a, radius, tau):
        s = v * tau + 0.5 * a * tau * tau
        if kind == "line":
            return (pos[0] + math.cos(hd) * s, pos[1] + math.sin(hd) * s), hd, v + a * tau
        sign = 1 if radius > 0 else -1
        r = abs(radius)
        cx, cy = pos[0] - sign * math.sin(hd) * r, pos[1] + sign * math.cos(hd) * r
        phi0 = math.atan2(pos[1] - cy, pos[0] - cx)
        phi = phi0 + sign * s / r
        return (cx + r * math.cos(phi), cy + r * math.sin(phi)), hd + sign * s / r, v + a * tau

    def truth(self, t):
        for t0, t1, kind, pos, hd, v, a, radius in self.pieces:
            if t0 <= t < t1 or t == t1 == self.duration:
                if kind == "rest":
                    return pos, 0.0, 0.0
                p, _, vv = self._advance(kind, pos, hd, v, a, radius, t - t0)
                return p, vv, a
        raise ValueError(t)

    def path_length(self, t0, t1, step=1e-3):
        n = max(1, int((t1 - t0) / step))
        return sum(self.truth(t0 + (i + 0.5) * (t1 - t0) / n)[1] for i in range(n)) * (t1 - t0) / n


def scenarios(split: str) -> list[dict]:
    """Named scenarios; the held-out split uses other seeds, speeds, places and camera geometry than the development split."""
    dev = split == "development"
    k = 1.0 if dev else 1.15
    near, far = ((10.0, 3.0), (10.0, 17.0)) if dev else ((14.0, 4.0), (26.0, 16.0))
    s = []
    s.append({"name": "stationary", "path": Path2(near, 0, [("rest", 8)]), "where": "near"})
    s.append({"name": "constant speed, near", "path": Path2((near[0] - 8, near[1]), 0, [("line", 6, 0.0)]), "v0": 4.0 * k, "where": "near"})
    s.append({"name": "constant speed, far (perspective)", "path": Path2((far[0] - 8, far[1]), 0, [("line", 6, 0.0)]), "v0": 4.0 * k, "where": "far"})
    s.append({"name": "accelerate and decelerate", "path": Path2((near[0] - 10, near[1] + 2), 0, [("rest", 1.5), ("line", 2.0, 2.5 * k), ("line", 2.0, 0.0), ("line", 2.0, -2.5 * k), ("rest", 1.5)]), "where": "near"})
    s.append({"name": "turn (circular arc)", "path": Path2((near[0], near[1] + 3), 0, [("line", 1.5, 2.0), ("arc", 4.5, 0.0, 3.0 * (1 if dev else -1)), ("line", 2.0, 0.0)]), "v0": 3.0, "where": "near"})
    s.append({"name": "sharp cut (2 m radius)", "path": Path2((near[0] - 4, near[1] + 4), 0, [("line", 2.0, 0.0), ("arc", 1.6, 0.0, 2.0), ("line", 2.0, 0.0)]), "v0": 3.5 * k, "where": "near"})
    s.append({"name": "far-side sprint start", "path": Path2((far[0] - 10, far[1]), 0, [("rest", 1.5), ("line", 2.5, 3.0 * k), ("line", 1.5, 0.0)]), "where": "far"})
    return s


def _camera(split: str):
    from camera_adapter.synthetic_truth import Camera  # noqa: PLC0415
    return Camera() if split == "development" else Camera(position=(22.0, -18.0, 11.0), target=(22.0, 10.0, 0.0), focal_px=430.0)


def _observe(path: Path2, camera, *, noise_px, seed, occlusion=None, gaps=(), track_change_at=None, fps=FPS_EST):
    """Observations of the path through the camera: Gaussian pixel noise, optional occlusion bias (a vertical shift of the
    observed ground point by `occlusion[2]` px with a lowered score between occlusion[0] and occlusion[1] s), dropped intervals."""
    rnd = random.Random(seed)
    obs = []
    n = int(path.duration * fps + 1e-9)
    for f in range(n + 1):
        t = f / fps
        if any(a <= t < b for a, b in gaps):
            continue
        (x, y), _, _ = path.truth(t)
        u, v = camera.project(x, y)
        conf = 0.95
        if occlusion and occlusion[0] <= t < occlusion[1]:
            v -= occlusion[2]
            conf = 0.55
        u, v = u + rnd.gauss(0, noise_px), v + rnd.gauss(0, noise_px)
        tid = "trk-1" if track_change_at is None or t < track_change_at else "trk-2"
        obs.append(Observation(f, round(t, 6), tid, u, v, conf, "athlete-1"))
    return obs


def _build(sc):
    """Scenarios that start moving are rebuilt with their initial speed folded into the first piece."""
    v0 = sc.get("v0")
    if not v0:
        sc["_truth"] = sc["path"].truth
        return sc
    p = sc["path"]
    start = p.pieces[0][3]
    hd = math.degrees(p.pieces[0][4])
    rebuilt = Path2.__new__(Path2)
    rebuilt.pieces, t, pos, h, v = [], 0.0, start, math.radians(hd), v0
    for (t0, t1, kind, _, _, _, a, radius) in p.pieces:
        dur = t1 - t0
        rebuilt.pieces.append((t, t + dur, kind, pos, h, v, a, radius))
        pos, h, v = Path2._advance(kind, pos, h, v, a, radius, dur)
        t += dur
    rebuilt.duration, rebuilt.changes = t, [pc[0] for pc in rebuilt.pieces[1:]]
    sc["path"], sc["_truth"] = rebuilt, rebuilt.truth
    sc.pop("v0")
    return sc


def _calibration(camera):
    from camera_adapter.synthetic_truth import FIELD_X, FIELD_Y  # noqa: PLC0415
    marks = [(0, 0), (FIELD_X, 0), (FIELD_X, FIELD_Y), (0, FIELD_Y), (FIELD_X / 2, 0), (FIELD_X / 2, FIELD_Y)]
    pts = [{"label": f"m{i}", "image": list(camera.project(*m)), "field": list(m)} for i, m in enumerate(marks)]
    return cal.fit(960, 540, pts, [], camera_id="estimator-eval")


def _variants(sc, split):
    """Each scenario with clean observations, pixel noise, partial-occlusion bias, a longer gap and a track change."""
    seed0 = 1000 if split == "development" else 7000
    d = sc["path"].duration
    return [("clean", dict(noise_px=0.0, seed=seed0)),
            ("noise 0.5 px", dict(noise_px=0.5, seed=seed0 + 1)),
            ("noise 1.5 px", dict(noise_px=1.5, seed=seed0 + 2)),
            ("partial occlusion (3 px bias, 1 s)", dict(noise_px=0.5, seed=seed0 + 3, occlusion=(d * 0.45, d * 0.45 + 1.0, 3.0))),
            ("1.0 s gap", dict(noise_px=0.5, seed=seed0 + 4, gaps=[(d * 0.5, d * 0.5 + 1.0)])),
            ("track change", dict(noise_px=0.5, seed=seed0 + 5, track_change_at=d * 0.55))]


def evaluate_config(est: EstimatorConfig, split: str) -> dict:
    camera = _camera(split)
    calib = _calibration(camera)
    acfg = AdapterConfig(estimator=est)
    mapping = TimeMapping(T0)
    rows = []
    t_proc = 0.0
    n_obs = 0
    for sc in [_build(x) for x in scenarios(split)]:
        path = sc["path"]
        for vname, kw in _variants(sc, split):
            obs = _observe(path, camera, **kw)
            n_obs += len(obs)
            a = time.perf_counter()
            res = process(ObservationFile(Video(960, 540, FPS_EST, "analytic"), "analytic", obs), calib, acfg)
            t_proc += time.perf_counter() - a
            samples = [r["sample"] for r in records(res, mapping, acfg, "eval")]
            pos_e, spd_e, acc_e, spd_change, acc_change = [], [], [], [], []
            for o, s in zip(res.outputs, samples):
                (x, y), v, acc = path.truth(o.video_time_s)
                near = any(o.video_time_s - est.window_s - 1e-9 <= c <= o.video_time_s + 1e-9 for c in path.changes)
                occluded = "occlusion" in kw and kw["occlusion"][0] <= o.video_time_s <= kw["occlusion"][1] + est.window_s
                pos_e.append(math.dist((s["x_position"], s["y_position"]), (x, y)))
                (spd_change if near or occluded else spd_e).append(s["speed_mps"] - v)
                (acc_change if near or occluded else acc_e).append(s["acceleration_mps2"] - acc)
            out_times = [o.video_time_s for o in res.outputs]
            unavailable = _unavailable(out_times, path.duration)
            lag = _lags(path, res.outputs, samples, est)
            truth_len = path.path_length(0, path.duration)
            dist = engine_distance(samples, T0, 0.0, path.duration)
            rows.append({"scenario": sc["name"], "variant": vname, "samples": len(samples), "skipped": res.skipped, "segments": len(res.segments),
                         "first_sample_s": round(out_times[0], 3) if out_times else None, "unavailable_s": round(unavailable, 3),
                         "position_m": stats(pos_e), "speed_mps": stats(spd_e), "accel_mps2": stats(acc_e),
                         "speed_near_changes_mps": stats(spd_change), "accel_near_changes_mps2": stats(acc_change), "lag_s": lag,
                         "distance": {**dist, "truth_m": round(truth_len, 3),
                                      "error_m": None if dist["estimated_distance_m"] is None else round(dist["estimated_distance_m"] - truth_len, 3),
                                      "truth_over_included_time_m": round(_truth_over_included(path, samples), 3)}})
    return {"config": asdict(est), "split": split, "rows": rows, "throughput_observations_per_s": round(n_obs / t_proc, 1) if t_proc else None}


UNAVAILABLE_GAP_S = 0.25


def _unavailable(times, duration):
    """Seconds without an estimate: before the first output, after the last, and every interval between outputs longer than
    UNAVAILABLE_GAP_S (the 10 Hz output cap alone never counts)."""
    if not times:
        return duration
    return times[0] + (duration - times[-1]) + sum(b - a for a, b in zip(times, times[1:]) if b - a > UNAVAILABLE_GAP_S)


def _truth_over_included(path, samples):
    """Analytic path length over exactly the intervals the engine would include (same segment, gap <= 3 s, both confident)."""
    total = 0.0
    prev = None
    for s in samples:
        t = (datetime.fromisoformat(s["timestamp"].replace("Z", "+00:00")) - T0).total_seconds()
        if prev and prev[1] == s.get("segment_id") and 0 < t - prev[0] <= 3.0 and prev[2] >= 0.5 and s["tracking_confidence"] >= 0.5:
            total += path.path_length(prev[0], t, step=5e-3)
        prev = (t, s.get("segment_id"), s["tracking_confidence"])
    return total


def _lags(path, outputs, samples, est):
    """Seconds after each speed change until the estimated speed stays within 0.2 m/s of the truth for 3 samples (None: never)."""
    rows = [(o.video_time_s, s["speed_mps"] - path.truth(o.video_time_s)[1]) for o, s in zip(outputs, samples)]
    lags = []
    for c in path.changes:
        later = [r for r in rows if r[0] >= c]
        lag = None
        for i, (t, _) in enumerate(later):
            if len(later[i:i + 3]) == 3 and all(abs(x[1]) <= 0.2 for x in later[i:i + 3]):
                lag = round(t - c, 3)
                break
        lags.append(lag)
    return lags


CONFIGS = {
    "window 0.3 s": EstimatorConfig(window_s=0.3, min_points=5, min_span_s=0.18, max_gap_s=0.3),
    "window 0.5 s (current default)": EstimatorConfig(),
    "window 0.8 s": EstimatorConfig(window_s=0.8, min_points=5, min_span_s=0.48),
    "window 1.0 s": EstimatorConfig(window_s=1.0, min_points=5, min_span_s=0.6),
}


def summarize(result: dict) -> dict:
    rows = result["rows"]
    def agg(key, sub="median_abs", filt=lambda r: True):
        vals = [r[key][sub] for r in rows if r.get(key) and filt(r)]
        return round(statistics.median(vals), 4) if vals else None
    clean = lambda r: r["variant"] in ("noise 0.5 px", "noise 1.5 px")   # noqa: E731
    lag_vals = [l for r in rows if r["variant"] == "clean" for l in r["lag_s"] if l is not None]     # response lag without pixel noise
    never = sum(1 for r in rows if r["variant"] == "clean" for l in r["lag_s"] if l is None)
    stationary = {r["variant"]: r["distance"]["estimated_distance_m"] for r in rows if r["scenario"] == "stationary"}
    dist_err = [abs(r["distance"]["estimated_distance_m"] - r["distance"]["truth_over_included_time_m"]) for r in rows if r["distance"]["estimated_distance_m"] is not None]
    return {"position_median_m": agg("position_m", filt=clean), "position_p95_m": agg("position_m", "p95_abs", clean),
            "speed_steady_clean_p95_mps": agg("speed_mps", "p95_abs", lambda r: r["variant"] == "clean"),
            "speed_steady_median_mps": agg("speed_mps", filt=clean), "speed_steady_p95_mps": agg("speed_mps", "p95_abs", clean),
            "accel_steady_median_mps2": agg("accel_mps2", filt=clean), "accel_steady_p95_mps2": agg("accel_mps2", "p95_abs", clean),
            "speed_near_changes_p95_mps": agg("speed_near_changes_mps", "p95_abs"),
            "first_sample_median_s": round(statistics.median([r["first_sample_s"] for r in rows if r["first_sample_s"] is not None]), 3),
            "unavailable_median_s": round(statistics.median([r["unavailable_s"] for r in rows]), 3),
            "lag_median_s": round(statistics.median(lag_vals), 3) if lag_vals else None, "lag_p95_s": round(sorted(lag_vals)[min(len(lag_vals) - 1, math.ceil(0.95 * len(lag_vals)) - 1)], 3) if lag_vals else None,
            "lag_never_settled_clean": never,
            "stationary_phantom_distance_m": stationary,
            "included_fraction_median": round(statistics.median([r["distance"]["included_fraction"] for r in rows]), 4),
            "distance_error_vs_truth_over_included_median_m": round(statistics.median(dist_err), 3) if dist_err else None,
            "distance_error_vs_truth_over_included_max_m": round(max(dist_err), 3) if dist_err else None,
            "throughput_observations_per_s": result["throughput_observations_per_s"]}


DEFAULT_NAME = "window 0.5 s (current default)"
SELECTION_RULE = ("Fixed before the held-out split is computed and applied to the development split only: replace the current default "
                  "only if a candidate is better on BOTH steady-speed p95 under noise and distance error against the truth over "
                  "included time, while its clean response-lag p95 stays at or below 0.5 s and no clean speed change is left unsettled. "
                  "The held-out split is then reported for every configuration, without re-selection.")


def select(dev: dict) -> dict:
    base = dev[DEFAULT_NAME]
    eligible = []
    for name, m in dev.items():
        if name == DEFAULT_NAME:
            continue
        better = m["speed_steady_p95_mps"] < base["speed_steady_p95_mps"] and m["distance_error_vs_truth_over_included_median_m"] < base["distance_error_vs_truth_over_included_median_m"]
        responsive = m["lag_p95_s"] is not None and m["lag_p95_s"] <= 0.5 and m["lag_never_settled_clean"] == 0
        eligible.append({"config": name, "better_on_noise_and_distance": better, "lag_p95_within_0.5_s": responsive, "qualifies": better and responsive})
    winners = [e["config"] for e in eligible if e["qualifies"]]
    return {"rule": SELECTION_RULE, "candidates": eligible, "decision": winners[0] if len(winners) == 1 else DEFAULT_NAME,
            "reason": "no candidate met every condition; the current default stays" if not winners else ("one candidate met every condition" if len(winners) == 1 else "several candidates qualified; the default stays until a further rule decides")}


def estimator_sweep(out) -> dict:
    """Compare CONFIGS on the development split, then report every one on the held-out split (no re-selection there)."""
    report = {"configs": {k: asdict(v) for k, v in CONFIGS.items()}, "development": {}, "held_out": {}, "details": {}}
    for name, est in CONFIGS.items():
        dev = evaluate_config(est, "development")
        held = evaluate_config(est, "held_out")
        report["development"][name] = summarize(dev)
        report["held_out"][name] = summarize(held)
        report["details"][name] = {"development": dev["rows"], "held_out": held["rows"]}
        out(f"  {name}: done")
    report["selection"] = select(report["development"])
    report["difficult_cases"] = _difficult(report)
    return report


def _difficult(report):
    """The worst rows of the current default on the held-out split, by speed and position error, for the report's attention."""
    rows = report["details"]["window 0.5 s (current default)"]["held_out"]
    worst_speed = sorted((r for r in rows if r["speed_near_changes_mps"]), key=lambda r: -r["speed_near_changes_mps"]["max_abs"])[:5]
    worst_pos = sorted((r for r in rows if r["position_m"]), key=lambda r: -r["position_m"]["max_abs"])[:5]
    return {"speed": [{"scenario": r["scenario"], "variant": r["variant"], "max_abs_mps": r["speed_near_changes_mps"]["max_abs"]} for r in worst_speed],
            "position": [{"scenario": r["scenario"], "variant": r["variant"], "max_abs_m": r["position_m"]["max_abs"]} for r in worst_pos]}
