"""Compare camera processing settings on the GENERATED synthetic clip (software behaviour, not real-match accuracy).

USAGE
    .venv/bin/python scripts/camera_settings_compare.py --out DIR [--variants baseline,every2,tiny,tiny-every2] [--queue-size 2]

THE CLIP: the same 1280x720, 25 fps, 20 s scene as scripts/video_pilot_check.py (camera_adapter/synthetic_people.py, with
the deliberate 1 s picture loss at 10.0-11.0 s), rendered once and used unchanged by every variant: the source video and
its frame times are fixed. Identities come from the simulated operator (reads the synthetic truth), assigned afresh for
each variant's own tracks: raw track ids are never carried between variants.

VARIANTS (local, pinned models only; nothing is downloaded)
  baseline      YOLOX-S, 640 detector input (its fixed export size), every frame       scripts/examples/camera/video_tracking_config_example.json
  every2        YOLOX-S, 640, every other frame deliberately skipped (--every-nth 2)   same config
  tiny          YOLOX-Tiny, 416 detector input, every frame                            scripts/examples/camera/video_tracking_config_tiny.json
  tiny-every2   YOLOX-Tiny, 416, every other frame
  (YOLOX-S cannot run at a lower input size: its ONNX export is 640 and camera_track_video.py refuses a mismatch; the
  smaller input is only available through the separate Tiny model.)

FOR EACH VARIANT
  recorded  every frame in order (camera_track_video.py; every-nth does not apply): processing rate; tracked coverage,
            position and speed error against the truth for red (near, the wearer), blue (middle) and yellow (FAR and
            small, legs hidden for part of its run).
  paced     camera_track_video.py --paced-replay at the file's own rate through the bounded queue: source rate, frames
            deliberately skipped, frames dropped because processing fell behind, processed rate, processing lag
            (frame available -> result ready), and the same coverage and error figures.
  Detection coverage per figure counts source frames where the figure's feet are in view and an observation lies within
  the tolerance of its true foot point; coordinates are mapped back to the full 1280x720 image by the detector code, so
  the position error against the truth also checks that mapping.
Then, for the variant you name with --best (default: decided after the table), the picture-loss and identity-uncertainty
checks of video_pilot_check.py run on its recorded output.
Output: DIR/compare.json and DIR/compare.md. Generated files stay in DIR (keep it out of git).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

PY = sys.executable
VARIANTS = {
    "baseline": {"model": "yolox_s", "config": "scripts/examples/camera/video_tracking_config_example.json", "every_nth": 1},
    "every2": {"model": "yolox_s", "config": "scripts/examples/camera/video_tracking_config_example.json", "every_nth": 2},
    "tiny": {"model": "yolox_tiny", "config": "scripts/examples/camera/video_tracking_config_tiny.json", "every_nth": 1},
    "tiny-every2": {"model": "yolox_tiny", "config": "scripts/examples/camera/video_tracking_config_tiny.json", "every_nth": 2},
}
FIGS = {"red": "vx1", "blue": "vx2", "yellow": "vx3"}


def load_vpc():
    spec = importlib.util.spec_from_file_location("video_pilot_check", ROOT / "scripts" / "video_pilot_check.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.ATHLETES = dict(FIGS)   # the far, small yellow figure is assigned too in this comparison
    return m


def coverage(obs, sp, camera, frames_considered=None) -> dict:
    """Per figure: share of in-view source frames with an observation within tolerance of the true foot point."""
    by_frame: dict[int, list] = {}
    for o in obs.observations:
        by_frame.setdefault(o.frame, []).append(o)
    out = {}
    n = int(round(sp.DURATION_S * sp.FPS))
    for name in FIGS:
        seen = total = 0
        for f in range(n):
            t = f / sp.FPS
            if not sp.in_view(camera, name, t, 20):
                continue
            total += 1
            (x, y), *_ = sp.truth(name, t)
            p = camera.project(x, y)
            h = camera.project3(x, y, 0)[1] - camera.project3(x, y, sp.FIGURE_HEIGHT_M)[1]
            if any(math.dist((o.u_px, o.v_px), p) <= max(15.0, 0.25 * h) for o in by_frame.get(f, [])):
                seen += 1
        out[name] = round(seen / total, 3) if total else None
    return out


def main(argv=None, out=print) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--variants", default=",".join(VARIANTS))
    ap.add_argument("--queue-size", type=int, default=2)
    ap.add_argument("--best", help="variant for the picture-loss and identity-uncertainty checks (default: none)")
    args = ap.parse_args(argv)
    d = args.out
    if d.exists() and any(d.iterdir()):
        out(f"REFUSED: {d} is not empty"); return 1
    d.mkdir(parents=True, exist_ok=True)
    vpc = load_vpc()
    from camera_adapter import observations as obsmod  # noqa: PLC0415
    from camera_adapter import synthetic_people as sp  # noqa: PLC0415
    from camera_adapter.evaluation import PEOPLE_CHECK_MARKS, PEOPLE_FIT_MARKS  # noqa: PLC0415
    for v in args.variants.split(","):
        m = VARIANTS[v]["model"]
        if not (ROOT / "models" / f"{m}.onnx").is_file():
            out(f"MISSING MODEL: models/{m}.onnx (copy it before going offline; nothing is downloaded)"); return 1
    camera = sp.Camera3()
    clip = d / "synthetic-people.avi"
    sp.render(clip, camera)
    vpc.blank_picture(clip, vpc.GAP_FRAMES)
    mark = lambda x, y: {"label": f"mark ({x}, {y})", "image": [round(c) for c in camera.project(x, y)], "field": [x, y]}  # noqa: E731
    (d / "points.json").write_text(json.dumps({"format": "athletrix-calibration-points", "format_version": 1, "camera_id": "synthetic-people-camera",
                                               "image": {"width": sp.WIDTH, "height": sp.HEIGHT}, "fit_points": [mark(*m) for m in PEOPLE_FIT_MARKS],
                                               "check_points": [mark(*m) for m in PEOPLE_CHECK_MARKS]}, indent=1))
    steps: list = []
    if vpc.sh([PY, "scripts/camera_calibrate.py", "fit", str(d / "points.json"), "--out", str(d / "calibration.json")], steps):
        out("calibration failed"); return 1
    cfg = json.loads((ROOT / "scripts/examples/camera/adapter_config_example.json").read_text())
    cfg.update(device_id="synthetic-people-cam-1", source="simulated")
    (d / "adapter-config.json").write_text(json.dumps(cfg, indent=1))
    common = ["--calibration", str(d / "calibration.json"), "--config", str(d / "adapter-config.json")]
    anchor = datetime.fromisoformat(vpc.FIXED_ANCHOR.replace("Z", "+00:00"))
    rows = []
    for v in args.variants.split(","):
        spec = VARIANTS[v]
        model = str(ROOT / "models" / f"{spec['model']}.onnx")
        res = {"variant": v, **spec}
        for mode in ("recorded", "paced"):
            if mode == "recorded" and spec["every_nth"] != 1:
                continue   # recorded processing handles every frame; the every-nth variants are measured paced
            tag = f"{v}-{mode}"
            cmd = [PY, "scripts/camera_track_video.py", str(clip), "--model", model, "--config", spec["config"], "--out", str(d / f"obs-{tag}.jsonl"), "--log", str(d / f"track-{tag}.json")]
            if mode == "paced":
                cmd += ["--paced-replay", "--queue-size", str(args.queue_size), "--every-nth", str(spec["every_nth"])]
            out(f"{tag}: tracking...")
            if vpc.sh(cmd, steps):
                out(f"{tag} failed: " + "; ".join(steps[-1]["output_tail"])); return 1
            obs = obsmod.load(d / f"obs-{tag}.jsonl")
            log = json.loads((d / f"track-{tag}.json").read_text())
            vpc.write_assignments(d / f"assign-{tag}.json", vpc.operator(obs, sp, camera, reconfirm=True))
            if vpc.sh([PY, "scripts/camera_to_samples.py", "export", str(d / f"obs-{tag}.jsonl"), *common, "--assignments", str(d / f"assign-{tag}.json"),
                       "--anchor-utc", vpc.FIXED_ANCHOR, "--out", str(d / f"samples-{tag}.jsonl"), "--log", str(d / f"adapter-{tag}.json")], steps):
                out(f"{tag} export failed: " + "; ".join(steps[-1]["output_tail"])); return 1
            samples = vpc.load_samples(d / f"samples-{tag}.jsonl")
            vs = vpc.compare_with_truth(samples, anchor, sp)
            entry = {"processing_fps": log["speed"].get("frames_per_second"), "detection_coverage": coverage(obs, sp, camera),
                     "tracked": {FIGS_R: {"coverage_of_clip": x["tracked_coverage_of_clip"], "position_error_m_median": (x["position_error_m"] or {}).get("median"),
                                          "position_error_m_p95": (x["position_error_m"] or {}).get("p95"), "speed_error_mps_median": (x["speed_error_mps"] or {}).get("median"),
                                          "identity_errors": x["identity_errors"], "samples": x["samples"]}
                                 for FIGS_R, x in ((vs[a]["figure"], vs[a]) for a in vs)},
                     "samples_inside_picture_loss": sum(1 for s in samples if vpc.GAP_FRAMES[0] / sp.FPS <= (datetime.fromisoformat(s["timestamp"].replace("Z", "+00:00")) - anchor).total_seconds() < vpc.GAP_FRAMES[1] / sp.FPS),
                     "model": (log.get("model") or {}).get("name"), "model_sha256": ((log.get("model") or {}).get("sha256") or "")[:16]}
            if mode == "paced":
                b = log.get("bounded", {})
                fr = b.get("frames", {})
                entry.update(source_fps=b.get("source_fps"), released=fr.get("released_by_source"), skipped_deliberately=fr.get("sampled_out"), dropped_behind=fr.get("dropped"),
                             processed=fr.get("processed"), lag_s=b.get("processing_lag_s"), kept_up=b.get("kept_up"), longest_drop_run_s=fr.get("longest_drop_run_s"))
            res[mode] = entry
        rows.append(res)
        out(json.dumps({k: res[k] for k in res if k in ("variant", "recorded", "paced")}, default=str)[:900])
    doc = {"label": "GENERATED clip (drawn figures; software behaviour only, not real-match accuracy); identities from a simulated operator", "variants": rows,
           "steps": steps}
    if args.best:
        tag = f"{args.best}-recorded" if VARIANTS[args.best]["every_nth"] == 1 else f"{args.best}-paced"
        obs = obsmod.load(d / f"obs-{tag}.jsonl")
        rec_assign = vpc.operator(obs, sp, camera, reconfirm=True)
        firsts = sorted(a.from_frame for a in rec_assign if a.athlete_id == FIGS["red"])
        import dataclasses as _dc  # noqa: PLC0415
        vpc.write_assignments(d / "assign-uncertain.json", [_dc.replace(a, identity="uncertain") if a.athlete_id == FIGS["red"] and firsts and a.from_frame > firsts[0] else a for a in rec_assign])
        vpc.write_assignments(d / "assign-no-reconfirm.json", vpc.operator(obs, sp, camera, reconfirm=False))
        chk = {}
        for name in ("uncertain", "no-reconfirm"):
            vpc.sh([PY, "scripts/camera_to_samples.py", "export", str(d / f"obs-{tag}.jsonl"), *common, "--assignments", str(d / f"assign-{name}.json"),
                    "--anchor-utc", vpc.FIXED_ANCHOR, "--out", str(d / f"samples-best-{name}.jsonl"), "--log", str(d / f"adapter-best-{name}.json")], steps)
            chk[name] = vpc.load_samples(d / f"samples-best-{name}.jsonl")
        full = vpc.load_samples(d / f"samples-{tag}.jsonl")
        vt = lambda s: (datetime.fromisoformat(s["timestamp"].replace("Z", "+00:00")) - anchor).total_seconds()  # noqa: E731
        red_gaps = vpc.compare_with_truth(full, anchor, sp)[FIGS["red"]]["gaps_over_0_35_s"]
        enc = next((g for g in red_gaps if g[1] <= vpc.GAP_FRAMES[0] / sp.FPS), None)
        doc["best"] = {"variant": args.best, "source": tag,
                       "picture_loss_samples_inside": sum(1 for s in full if vpc.GAP_FRAMES[0] / sp.FPS <= vt(s) < vpc.GAP_FRAMES[1] / sp.FPS),
                       "red_first_encounter_gap_s": enc,
                       "red_samples_full": sum(1 for s in full if s["athlete_id"] == FIGS["red"]),
                       "red_samples_uncertain_after_first_assignment": sum(1 for s in chk["uncertain"] if s["athlete_id"] == FIGS["red"] and enc and vt(s) > enc[0] + 0.05),
                       "red_samples_between_encounter_and_picture_loss_without_reconfirmation":
                           sum(1 for s in chk["no-reconfirm"] if s["athlete_id"] == FIGS["red"] and enc and enc[1] - 0.05 < vt(s) < vpc.GAP_FRAMES[0] / sp.FPS),
                       "red_samples_between_encounter_and_picture_loss_with_reconfirmation":
                           sum(1 for s in full if s["athlete_id"] == FIGS["red"] and enc and enc[1] - 0.05 < vt(s) < vpc.GAP_FRAMES[0] / sp.FPS)}
    (d / "compare.json").write_text(json.dumps(doc, indent=1, default=str))
    L = ["# Camera settings comparison (GENERATED clip)", "", doc["label"], "",
         "| variant | mode | processed fps | skipped | dropped | lag p95 s | red cov / err m | blue cov / err m | yellow (far) cov / err m | samples in picture loss |",
         "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        for mode in ("recorded", "paced"):
            if mode not in r:
                continue
            e = r[mode]
            t = e["tracked"]
            cell = lambda f: f"{t[f]['coverage_of_clip']} / {t[f]['position_error_m_median']}" if f in t else "-"  # noqa: E731
            L.append(f"| {r['variant']} | {mode} | {e['processing_fps']} | {e.get('skipped_deliberately', '-')} | {e.get('dropped_behind', '-')} | "
                     f"{(e.get('lag_s') or {}).get('p95', '-') if mode == 'paced' else '-'} | {cell('red')} | {cell('blue')} | {cell('yellow')} | {e['samples_inside_picture_loss']} |")
    if "best" in doc:
        L += ["", "## Picture loss and identity checks on the chosen variant", "", "```", json.dumps(doc["best"], indent=1), "```"]
    (d / "compare.md").write_text("\n".join(L) + "\n")
    out(f"wrote {d / 'compare.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
