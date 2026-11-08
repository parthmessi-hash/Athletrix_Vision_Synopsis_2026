"""Run the ACTUAL camera video path (decode -> person detection -> tracking -> calibration -> samples -> isolated pilot
session) on a GENERATED synthetic clip, and report what it delivered. No footage of any person; nothing is downloaded.

USAGE
    .venv/bin/python scripts/video_pilot_check.py --out DIR [--model models/yolox_s.onnx] [--queue-size 2]

THE CLIP: camera_adapter/synthetic_people.py draws five cartoon figures (red, blue, yellow, white, black) on a fictional
24 m x 14 m area, 1280x720, 25 fps, 20 s, with analytic truth (positions and speeds at any time). red is the fictional
vest wearer (vx1), blue a camera-only player (vx2); yellow, white and black are never assigned.

TWO PATHS, KEPT SEPARATE
  A. RECORDED VIDEO, IN ORDER (for later review): scripts/camera_track_video.py decodes every frame; the samples are
     exported with a fixed anchor (sample time = anchor + video time) and compared with the truth. Nothing is sent.
  B. PACED PLAYBACK (live-style): scripts/camera_track_video.py --paced-replay releases frames at the file's own rate
     through the bounded queue (frames the processor cannot take in time are DROPPED and stay gaps), then
     scripts/camera_to_samples.py send --anchor-utc now --speed 1 posts the samples at their own pace into an isolated
     engine with a FICTIONAL pilot session, while a FICTIONAL heart-rate feeder plays the vest. The engine's
     round-readiness state is polled throughout; a round start is attempted before the camera arrives (it must be
     refused), then made when the readiness says both streams are usable; the score is saved at the end.
     Processing and sending run one after the other (the existing tools work on files): this is a paced playback of a
     file, not a live camera, and the capture-to-ingest delay measured here is the send pacing plus the engine's
     receipt, not camera latency.
  Track identities come from a SIMULATED OPERATOR (a script reading the synthetic truth: confirmed_by
  "simulated_oracle"), at each track's first observation and again after each close encounter. A variant without the
  re-confirmation shows that an identity lapse removes that player's samples (score evidence) instead of guessing.

The scoring profile is a FICTIONAL short-window variant written into DIR (windows of 6 s so a 20 s clip can fill them);
it is not the team's rules. The model and every setting are recorded. Outputs stay in DIR (keep it out of git).

EXIT CODES: 0 the run completed (read the report for what it shows) · 1 missing model, dependency or a failed step
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

PY = sys.executable
ATHLETES = {"red": "vx1", "blue": "vx2"}   # yellow, white, black are never assigned
VEST = "fictional-video-check-vest"
FIXED_ANCHOR = "2026-11-08T10:00:00Z"


def z(t: datetime) -> str:
    return t.astimezone(UTC).isoformat().replace("+00:00", "Z")


def stats(v):
    if not v:
        return None
    s = sorted(v)
    return {"n": len(s), "median": round(statistics.median(s), 4), "p95": round(s[min(len(s) - 1, int(0.95 * len(s)))], 4), "max": round(s[-1], 4)}


def sh(cmd: list[str], log: list) -> int:
    t0 = time.perf_counter()
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
    log.append({"command": " ".join(str(c) for c in cmd).replace(str(ROOT) + "/", ""), "exit": r.returncode, "seconds": round(time.perf_counter() - t0, 2),
                "output_tail": (r.stdout + r.stderr).strip().splitlines()[-6:]})
    return r.returncode


def profile_doc() -> dict:
    p = json.loads((ROOT / "scripts/examples/pilot/fictional_rehearsal_profile.json").read_text())
    p["version"] = "fictional-video-check-short-windows-1"
    p["note"] = ("FICTIONAL 6-second-window variant written by scripts/video_pilot_check.py so a 20 s synthetic clip can fill the windows. "
                 "Not the team's rules; never for collecting validation data.")
    p["streams"]["staleness_s"] = 3
    p["reference"]["camera_reference_s"] = 6
    for k, f in p["features"].items():
        if "window_s" in f:
            f["window_s"] = 6
    p["features"]["hr_strain"]["min_samples"] = 4
    p["features"]["hr_per_speed"]["min_pairs"] = 4
    p["features"]["recovery"]["lookback_s"] = 20
    return p


def operator(obs, sp, camera, reconfirm: bool):
    """Simulated operator (reads the synthetic truth): assign red and blue tracks at their first observation and, with
    reconfirm, again after each close encounter; everything else stays unassigned."""
    from camera_adapter.observations import Assignment  # noqa: PLC0415

    def foot(name, t):
        st = sp.truth(name, t)
        return None if st is None else camera.project(*st[0])

    def height(name, t):
        (x, y), *_ = sp.truth(name, t)
        return camera.project3(x, y, 0)[1] - camera.project3(x, y, sp.FIGURE_HEIGHT_M)[1]

    def nearest(o):
        best = None
        for n in sp.FIGURES:
            p = foot(n, o.video_time_s)
            if p is not None and (best is None or math.dist((o.u_px, o.v_px), p) < best[1]):
                best = (n, math.dist((o.u_px, o.v_px), p))
        return best
    by_track: dict = {}
    for o in obs.observations:
        by_track.setdefault(o.track_id, []).append(o)
    out = []
    for tid, seq in sorted(by_track.items(), key=lambda kv: int(kv[0].split("-")[1])):
        starts = [seq[0]] + ([b for a, b in zip(seq, seq[1:]) if a.near_other_track and not b.near_other_track] if reconfirm else [])
        rows = []
        for o in starts:
            n, d = nearest(o)
            rows.append((o.frame, ATHLETES.get(n) if d <= max(15.0, 0.25 * height(n, o.video_time_s)) else None))
        for (f0, ath), nxt in zip(rows, rows[1:] + [(None, None)]):
            if ath:
                out.append(Assignment(tid, ath, f0, None if nxt[0] is None else nxt[0] - 1, "simulated operator (reads the synthetic truth)", confirmed_by="simulated_oracle"))
    return out


GAP_FRAMES = (250, 275)   # a deliberate 1 s loss of picture (10.0-11.0 s): plain field colour, no figures


def blank_picture(clip: Path, frames: tuple[int, int]) -> None:
    """Rewrite the clip with frames [a, b) replaced by a plain field-coloured picture (the camera sees nobody). The frame
    count and rate stay the same, so capture times are unchanged; nothing may be detected or invented in the gap."""
    import cv2  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415
    cap = cv2.VideoCapture(str(clip))
    fps, w, h = cap.get(cv2.CAP_PROP_FPS), int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    tmp = clip.with_name("tmp-" + clip.name)
    wr = cv2.VideoWriter(str(tmp), cv2.VideoWriter_fourcc(*"MJPG"), fps, (w, h))
    i = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if frames[0] <= i < frames[1]:
            frame = np.full_like(frame, (55, 115, 55))
        wr.write(frame)
        i += 1
    cap.release(); wr.release()
    tmp.replace(clip)


def write_assignments(path: Path, assignments) -> None:
    from dataclasses import asdict  # noqa: PLC0415
    path.write_text(json.dumps({"format": "athletrix-track-assignments", "format_version": 2,
                                "assignments": [{k: v for k, v in asdict(a).items() if v is not None} for a in assignments]}, indent=1))


def compare_with_truth(samples: list[dict], anchor: datetime, sp) -> dict:
    """Per assigned figure: tracked coverage of its in-view time, position and speed error, identity errors (a sample
    nearer another figure's true position than 1 m while more than 1.5 m from its own), and gaps between samples."""
    out = {}
    rev = {v: k for k, v in ATHLETES.items()}
    for aid, name in rev.items():
        mine = sorted((datetime.fromisoformat(s["timestamp"].replace("Z", "+00:00")), s) for s in samples if s["athlete_id"] == aid)
        pos_err, spd_err, ident, gaps = [], [], 0, []
        prev = None
        for t, s in mine:
            vt = (t - anchor).total_seconds()
            tr = sp.truth(name, vt)
            if tr is None:
                continue
            e = math.dist((s["x_position"], s["y_position"]), tr[0])
            others = [math.dist((s["x_position"], s["y_position"]), sp.truth(n, vt)[0]) for n in sp.FIGURES if n != name and sp.truth(n, vt)]
            if e > 1.5 and others and min(others) < 1.0:
                ident += 1
            pos_err.append(e)
            spd_err.append(abs(s["speed_mps"] - tr[3]))
            if prev is not None and vt - prev > 0.35:
                gaps.append([round(prev, 2), round(vt, 2)])
            prev = vt
        covered = 0.0
        times = [(t - anchor).total_seconds() for t, _ in mine]
        for a, b in zip(times, times[1:]):
            covered += min(b - a, 0.35)
        out[aid] = {"figure": name, "samples": len(mine), "first_s": round(times[0], 2) if times else None, "last_s": round(times[-1], 2) if times else None,
                    "tracked_coverage_of_clip": round(covered / sp.DURATION_S, 3), "position_error_m": stats(pos_err), "speed_error_mps": stats(spd_err),
                    "identity_errors": ident, "gaps_over_0_35_s": gaps}
    return out


def load_samples(path: Path) -> list[dict]:
    return [json.loads(x)["sample"] for x in path.read_text().splitlines() if x.strip()]


def main(argv=None, out=print) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--model", type=Path, default=ROOT / "models" / "yolox_s.onnx")
    ap.add_argument("--queue-size", type=int, default=2)
    ap.add_argument("--engine-port", type=int, help="fixed port for the isolated engine (for a dashboard copy watching path B)")
    ap.add_argument("--cors-origin", action="append", default=[], help="dashboard origin allowed to read the isolated engine (repeatable)")
    ap.add_argument("--hold-s", type=float, default=0.0, help="keep the engine up this long after the score is saved, for a browser look")
    args = ap.parse_args(argv)
    if args.out.exists() and any(args.out.iterdir()):
        out(f"REFUSED: {args.out} is not empty")
        return 1
    args.out.mkdir(parents=True, exist_ok=True)
    if not args.model.is_file():
        out(f"MISSING MODEL: {args.model} is not on this laptop. Copy it before going offline (scripts/fetch_person_model.py, run by a person); "
            "this check never downloads anything.")
        return 1
    try:
        from camera_adapter import synthetic_people as sp  # noqa: PLC0415
        from camera_adapter.evaluation import PEOPLE_CHECK_MARKS, PEOPLE_FIT_MARKS  # noqa: PLC0415
        from camera_adapter import observations as obsmod  # noqa: PLC0415
    except ImportError as exc:
        out(f"MISSING OPTIONAL DEPENDENCY ({exc.name}): see requirements-video.txt")
        return 1
    d, steps = args.out, []
    report: dict = {"label": "ACTUAL video software path on a GENERATED synthetic clip (drawn figures, no footage of any person); FICTIONAL heart rate; "
                             "identities from a simulated operator reading the synthetic truth",
                    "started_at": z(datetime.now(UTC)), "steps": steps}
    camera = sp.Camera3()
    clip = d / "synthetic-people.avi"
    sp.render(clip, camera)
    blank_picture(clip, GAP_FRAMES)
    report["clip"] = {"file": clip.name, "sha256": hashlib.sha256(clip.read_bytes()).hexdigest(), "fps": sp.FPS, "size": [sp.WIDTH, sp.HEIGHT], "duration_s": sp.DURATION_S,
                      "figures": list(sp.FIGURES), "assigned": ATHLETES}
    mark = lambda x, y: {"label": f"mark ({x}, {y})", "image": [round(c) for c in camera.project(x, y)], "field": [x, y]}  # noqa: E731
    (d / "points.json").write_text(json.dumps({"format": "athletrix-calibration-points", "format_version": 1, "camera_id": "synthetic-people-camera",
                                               "image": {"width": sp.WIDTH, "height": sp.HEIGHT}, "fit_points": [mark(*m) for m in PEOPLE_FIT_MARKS],
                                               "check_points": [mark(*m) for m in PEOPLE_CHECK_MARKS]}, indent=1))
    if sh([PY, "scripts/camera_calibrate.py", "fit", str(d / "points.json"), "--out", str(d / "calibration.json")], steps):
        out("calibration failed"); return 1
    cfg = json.loads((ROOT / "scripts/examples/camera/adapter_config_example.json").read_text())
    cfg.update(device_id="synthetic-people-cam-1", source="simulated")
    (d / "adapter-config.json").write_text(json.dumps(cfg, indent=1))
    (d / "profile.json").write_text(json.dumps(profile_doc(), indent=1))
    common = ["--calibration", str(d / "calibration.json"), "--config", str(d / "adapter-config.json")]

    # ---- A. recorded video, every frame in order ----
    out("A. recorded video: decoding and tracking every frame (YOLOX + ByteTrack)...")
    if sh([PY, "scripts/camera_track_video.py", str(clip), "--model", str(args.model), "--out", str(d / "obs-recorded.jsonl"), "--log", str(d / "track-recorded.json")], steps):
        out("tracking failed: " + "; ".join(steps[-1]["output_tail"])); return 1
    obs_a = obsmod.load(d / "obs-recorded.jsonl")
    tl_a = json.loads((d / "track-recorded.json").read_text())
    write_assignments(d / "assign-recorded.json", operator(obs_a, sp, camera, reconfirm=True))
    write_assignments(d / "assign-no-reconfirm.json", operator(obs_a, sp, camera, reconfirm=False))
    # identity UNCERTAIN after red's first close encounter: the re-confirmations are recorded as uncertain, not confirmed
    import dataclasses as _dc  # noqa: PLC0415
    rec_assign = operator(obs_a, sp, camera, reconfirm=True)
    red_first = {}
    for a in rec_assign:
        if a.athlete_id == ATHLETES["red"]:
            red_first.setdefault(a.track_id, a.from_frame)
    first_red = min(red_first.values()) if red_first else 0
    write_assignments(d / "assign-uncertain.json", [_dc.replace(a, identity="uncertain") if a.athlete_id == ATHLETES["red"] and a.from_frame > first_red else a for a in rec_assign])
    for tag, af in (("recorded", "assign-recorded.json"), ("no-reconfirm", "assign-no-reconfirm.json"), ("uncertain", "assign-uncertain.json")):
        if sh([PY, "scripts/camera_to_samples.py", "export", str(d / "obs-recorded.jsonl"), *common, "--assignments", str(d / af), "--anchor-utc", FIXED_ANCHOR,
               "--out", str(d / f"samples-{tag}.jsonl"), "--log", str(d / f"adapter-{tag}.json")], steps):
            out("export failed: " + "; ".join(steps[-1]["output_tail"])); return 1
    anchor = datetime.fromisoformat(FIXED_ANCHOR.replace("Z", "+00:00"))
    sa, sn = load_samples(d / "samples-recorded.jsonl"), load_samples(d / "samples-no-reconfirm.jsonl")
    report["A_recorded"] = {
        "model": tl_a.get("model"), "tracking_config": tl_a.get("config"), "versions": tl_a.get("versions"),
        "frames_decoded": tl_a["video"].get("decoded_frames"), "decoder_time_mismatches": tl_a["video"].get("decoder_time_mismatches"),
        "processing_fps": tl_a["speed"].get("frames_per_second"), "realtime_factor": tl_a["speed"].get("realtime_factor"),
        "detections": tl_a.get("detections"), "observations": tl_a.get("observations"), "tracks": len(tl_a.get("tracks") or {}),
        "time_mapping": f"sample time = {FIXED_ANCHOR} + video time (frame / fps); stated anchor, not capture time",
        "vs_truth": compare_with_truth(sa, anchor, sp),
        "identity_lapse_without_reconfirmation": {aid: {"samples_with_reconfirmation": sum(1 for s in sa if s["athlete_id"] == aid),
                                                        "samples_without": sum(1 for s in sn if s["athlete_id"] == aid),
                                                        "last_sample_without_s": max([(datetime.fromisoformat(s["timestamp"].replace("Z", "+00:00")) - anchor).total_seconds()
                                                                                      for s in sn if s["athlete_id"] == aid] or [None], key=lambda v: v or -1)}
                                                  for aid in ATHLETES.values()},
    }
    su = load_samples(d / "samples-uncertain.jsonl")
    vt = lambda s: (datetime.fromisoformat(s["timestamp"].replace("Z", "+00:00")) - anchor).total_seconds()  # noqa: E731
    g0, g1 = GAP_FRAMES[0] / sp.FPS, GAP_FRAMES[1] / sp.FPS
    for aid, v in report["A_recorded"]["vs_truth"].items():   # the window from the first encounter gap to the picture loss
        first_gap = next((g for g in v["gaps_over_0_35_s"] if g[1] <= g0), None)
        if first_gap:
            lo = first_gap[1] - 0.05
            report["A_recorded"]["identity_lapse_without_reconfirmation"][aid].update(
                window_s=[round(lo, 2), g0], samples_in_window_with_reconfirmation=sum(1 for s in sa if s["athlete_id"] == aid and lo < vt(s) < g0),
                samples_in_window_without=sum(1 for s in sn if s["athlete_id"] == aid and lo < vt(s) < g0),
                note="after the picture loss the tracker starts new tracks, which a first-observation assignment covers again")
    lapse = (report["A_recorded"]["vs_truth"][ATHLETES["red"]]["gaps_over_0_35_s"] or [[None]])[0][0]
    report["A_recorded"]["uncertain_identity"] = {
        "rule": "every red assignment after red's first one (re-confirmations after encounters, new tracks after the picture loss) recorded with identity 'uncertain'",
        "red_samples_confirmed_everywhere": sum(1 for s in sa if s["athlete_id"] == ATHLETES["red"]),
        "red_samples_with_uncertain_later": sum(1 for s in su if s["athlete_id"] == ATHLETES["red"]),
        "red_samples_after_the_encounter_with_uncertain": sum(1 for s in su if s["athlete_id"] == ATHLETES["red"] and lapse is not None and vt(s) > lapse + 0.05),
        "note": "an uncertain identity produces no samples for the athlete, so no camera or combined score can use it"}
    report["A_recorded"]["deliberate_picture_loss"] = {
        "frames": list(GAP_FRAMES), "video_s": [g0, g1],
        "observations_inside": sum(1 for o in obs_a.observations if GAP_FRAMES[0] <= o.frame < GAP_FRAMES[1]),
        "samples_inside_by_athlete": {aid: sum(1 for s in sa if s["athlete_id"] == aid and g0 <= vt(s) < g1) for aid in ATHLETES.values()},
        "first_sample_after_by_athlete": {aid: round(min([vt(s) for s in sa if s["athlete_id"] == aid and vt(s) >= g1] or [float("nan")]), 3) for aid in ATHLETES.values()},
        "note": "no detection is possible in the blank frames: any sample there would be invented"}
    ts_ok = all(abs(((datetime.fromisoformat(s["timestamp"].replace("Z", "+00:00")) - anchor).total_seconds() * sp.FPS) - round((datetime.fromisoformat(s["timestamp"].replace("Z", "+00:00")) - anchor).total_seconds() * sp.FPS)) < 1e-3
                for s in sa)
    report["A_recorded"]["sample_times_on_frame_times"] = ts_ok

    # ---- B. paced playback into an isolated pilot session ----
    out("B. paced playback: bounded processing at the file's own rate...")
    if sh([PY, "scripts/camera_track_video.py", str(clip), "--model", str(args.model), "--paced-replay", "--queue-size", str(args.queue_size),
           "--out", str(d / "obs-paced.jsonl"), "--log", str(d / "track-paced.json")], steps):
        out("paced tracking failed: " + "; ".join(steps[-1]["output_tail"])); return 1
    obs_b = obsmod.load(d / "obs-paced.jsonl")
    tl_b = json.loads((d / "track-paced.json").read_text())
    write_assignments(d / "assign-paced.json", operator(obs_b, sp, camera, reconfirm=True))
    import live_session  # noqa: PLC0415
    from validate_team_scale import IsolatedBackend  # noqa: PLC0415
    eng = IsolatedBackend(port=args.engine_port, extra_env={"FATIGUE_PROFILE_PATH": str(d / "profile.json")},
                          cors_origins=args.cors_origin or None)
    players = [("vx1", "Fictional video participant 1", 71, False, None, [], True)]
    rest_id, bases = live_session.prepare_database(eng.db_path, datetime.now(UTC), players=players)
    eng.start()
    c = httpx.Client(base_url=eng.base_url + "/api/v1", timeout=15.0)
    polls: list = []
    stop = threading.Event()
    try:
        for aid, n in (("vx1", 71), ("vx2", 72)):
            c.post("/athletes", json={"id": aid, "name": f"Fictional video participant {n - 70}", "jersey_number": n, "sport": "soccer", "team_id": "fictional-video-check"})
        rest = c.get(f"/recordings/{rest_id}").json()
        b = c.post("/athletes/vx1/baselines", json={"source_recording_id": rest_id, "ranges": [{"start": rest["started_at"], "end": rest["stopped_at"]}], "condition": "seated_rest",
                                                     "signals": ["heart_rate_bpm"], "min_samples": 60, "label": "FICTIONAL rest", "created_by": "video check"}).json()
        c.post(f"/athletes/vx1/baselines/{b['id']}/decision", json={"decision": "accepted", "decided_by": "video check", "note": "FICTIONAL"})
        rid = c.post("/recordings", json={"name": "FICTIONAL video path check", "athlete_ids": ["vx1", "vx2"]}).json()["id"]
        tok = iter(range(1000))

        def entry(**kw):
            return c.post(f"/recordings/{rid}/study", json={"client_token": f"video-check-{next(tok):04d}", **kw})
        entry(kind="session", session_label="video-check", protocol="rehearsal", fictional=True, planned_rounds=1,
              clock_references=[{"stream": "camera", "basis": "operator_estimate", "uncertainty_s": 0.04,
                                 "note": "FICTIONAL: paced playback; anchor = UTC when sending started minus the first sample's video time"}])
        entry(kind="wearer", athlete_id="vx1", participant_code="V1", device_id=VEST, baseline_id=b["id"], hr_max_bpm=200, hr_max_origin="FICTIONAL")
        rest_hr = bases["vx1"]["hr"]
        send_start: dict = {}

        def feeder():
            while not stop.wait(1.0):
                t = datetime.now(UTC)
                vt = (t - send_start["anchor"]).total_seconds() if "anchor" in send_start else 0.0
                tr = sp.truth("red", min(max(vt, 0.0), sp.DURATION_S))
                c.post("/samples/physiological", json={"athlete_id": "vx1", "timestamp": z(t), "source": "simulated", "device_id": VEST,
                                                       "heart_rate_bpm": round(rest_hr + 60 + 12 * (tr[3] if tr else 0), 1)})

        def poller():
            while not stop.wait(0.5):
                r = c.get(f"/recordings/{rid}/study/readiness", params={"athlete_id": "vx1"})
                if r.status_code == 200:
                    x = r.json()
                    polls.append({"at": x["at"], "state": x["state"], "camera": (x.get("streams") or {}).get("camera", {}).get("detail"),
                                  "wearable": (x.get("streams") or {}).get("wearable", {}).get("detail")})
        threading.Thread(target=feeder, daemon=True).start()
        threading.Thread(target=poller, daemon=True).start()
        time.sleep(3.0)
        early = entry(kind="round_start", athlete_id="vx1", round=1)
        report_b_early = {"status": early.status_code, "state": ((early.json().get("detail") or {}).get("readiness") or {}).get("state") if early.status_code == 409 else None}
        import camera_to_samples  # noqa: PLC0415
        send_lines: list = []
        res: dict = {}

        def sender():
            res["exit"] = camera_to_samples.main(["send", str(d / "obs-paced.jsonl"), *common, "--assignments", str(d / "assign-paced.json"), "--anchor-utc", "now",
                                                  "--url", eng.base_url, "--speed", "1", "--log", str(d / "adapter-paced.json")], out=send_lines.append,
                                                 now=lambda: send_start.setdefault("now", datetime.now(UTC)))
        th = threading.Thread(target=sender)
        th.start()
        started = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and started is None:
            if "now" in send_start and "anchor" not in send_start:
                send_start["anchor"] = send_start["now"]   # approximate: the feeder only needs red's phase for the FICTIONAL heart rate
            r = c.get(f"/recordings/{rid}/study/readiness", params={"athlete_id": "vx1"}).json()
            if r["state"] in ("ready_to_start",):
                started = entry(kind="round_start", athlete_id="vx1", round=1)
                break
            time.sleep(0.2)
        th.join()
        alog = json.loads((d / "adapter-paced.json").read_text())
        def view():   # what the dashboard's pilot card renders (GET .../study/pilot)
            v = c.get(f"/recordings/{rid}/study/pilot", params={"trend_span_s": 0}).json()
            e = v.get("estimate") or {}
            return {"wearer": (v.get("wearer") or {}).get("athlete_id"), "camera_stream": (e.get("streams") or {}).get("camera"),
                    "camera_only": {k: (e.get("candidates") or {}).get("camera_only", {}).get(k) for k in ("available", "value", "reasons")},
                    "combined": {k: (e.get("candidates") or {}).get("combined", {}).get(k) for k in ("available", "value", "reasons")},
                    "readiness": (v.get("readiness") or {}).get("state")}
        view_live = view()
        save = entry(kind="score_save", athlete_id="vx1", round=1).json()
        if args.hold_s:
            out(f"holding {args.hold_s:g} s for a browser look: engine {eng.base_url}, recording {rid}")
        time.sleep(4.0)   # the camera has stopped; the vest continues
        after = c.get(f"/recordings/{rid}/study/readiness", params={"athlete_id": "vx1"}).json()
        view_after = view()
        if args.hold_s > 4:
            time.sleep(args.hold_s - 4)
        stop.set()
        time.sleep(0.6)
        c.post(f"/recordings/{rid}/stop")
        # receipts: engine receipt minus the sample's own (anchor-mapped) time
        con = sqlite3.connect(eng.db_path)
        delays = []
        for ts, rc in con.execute("SELECT ts_utc, receipt FROM recording_tracking_samples WHERE recording_id = ?", (rid,)):
            if rc:
                delays.append((datetime.fromisoformat(json.loads(rc)["received_at"].replace("Z", "+00:00")) - datetime.fromisoformat(ts.replace("Z", "+00:00"))).total_seconds())
        n_track = {a: con.execute("SELECT COUNT(*) FROM recording_tracking_samples WHERE recording_id = ? AND athlete_id = ?", (rid, a)).fetchone()[0] for a in ATHLETES.values()}
        con.close()
        bl = tl_b.get("bounded", {})
        states = []
        for p_ in polls:
            if not states or states[-1]["state"] != p_["state"]:
                states.append(p_)
        mapping = alog.get("time_mapping") or {}
        session_ref = next((e.get("clock_references") for e in c.get(f"/recordings/{rid}/study").json()["entries"] if e["kind"] == "session"), None)
        pred = save.get("prediction") or {}
        cands = (pred.get("result") or {}).get("candidates") or {}
        report["B_paced"] = {
            "model": tl_b.get("model"), "queue_size": args.queue_size,
            "frames": bl.get("frames"), "source_fps": bl.get("source_fps"), "processing_fps": tl_b["speed"].get("frames_per_second"),
            "processing_lag_s": bl.get("processing_lag_s"), "kept_up": bl.get("kept_up"),
            "time_mapping": mapping, "send": send_lines[-4:], "send_exit": res.get("exit"),
            "engine_tracking_samples": n_track, "capture_to_ingest_s": stats(delays),
            "capture_to_ingest_note": "engine receipt minus the sample's anchor-mapped video time: the send pacing plus HTTP and the engine's receipt; "
                                      "processing happened before sending, so this is not camera-to-engine latency",
            "early_round_start": report_b_early, "round_start": started.json() if started is not None else None,
            "readiness_transitions": states, "readiness_after_camera_stopped": {"state": after["state"], "next": after["next"]},
            "pilot_view_at_end_of_clip": view_live, "pilot_view_4s_after_camera_stopped": view_after,
            "saved_score": {"id": save.get("id"), "all_three_available": pred.get("all_three_available"),
                            "candidates": {k: (v["value"] if v["available"] else "not available: " + (v["reasons"] or ["?"])[0]) for k, v in cands.items()},
                            "hr_per_speed": ((pred.get("result") or {}).get("features") or {}).get("hr_per_speed", {}).get("reason") or "available",
                            "session_camera_clock_reference": session_ref},
        }
        if started is not None:
            rs = started.json()
            report["B_paced"]["round_start"] = {k: rs.get(k) for k in ("id", "at", "opens_camera_reference")}
    finally:
        stop.set()
        c.close()
        eng.stop()
        eng.cleanup()
    report["finished_at"] = z(datetime.now(UTC))
    (d / "report.json").write_text(json.dumps(report, indent=1, default=str))
    out(json.dumps({k: report[k] for k in ("A_recorded", "B_paced")}, indent=1, default=str)[:6000])
    out(f"wrote {d / 'report.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
