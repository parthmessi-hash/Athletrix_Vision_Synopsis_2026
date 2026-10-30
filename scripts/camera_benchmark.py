"""Benchmark local person-tracking configurations honestly: offline file analysis versus bounded paced replay.

USAGE
    python scripts/camera_benchmark.py [--video FILE] [--models yolox_s,yolox_tiny] [--every-nth 1,2,3] [--report PATH]

Without --video the drawn-figure scene (camera_adapter/synthetic_people.py, fictional) is rendered to a temporary
folder. For every available pinned model (models/<name>.onnx; missing ones are reported, never downloaded):
  offline   every frame as fast as the machine allows: source duration, processing duration, throughput, realtime
            factor and whether it kept up with the file's own rate (observation times stay video-relative);
  paced     the file released at its own frame rate through the bounded queue (queue of 2, oldest dropped), once per
            --every-nth value: frames processed, sampled out and dropped, processing lag (frame available -> result
            ready; not engine receipt or display delay), observations, and whether processing kept up.
The report records the machine, OpenCV's thread count, package versions and each model's SHA-256. A paced replay of a
file is not a camera: it shows whether this machine keeps pace with this frame size and rate, nothing about a camera's
own timing. Writes reports/ (ignored by git).

EXIT CODES: 0 ran · 1 no model available or a run failed · 2 bad arguments or missing packages
"""

import argparse
import json
import platform
import sys
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CONFIGS = {"yolox_s": ROOT / "scripts" / "examples" / "camera" / "video_tracking_config_example.json",
           "yolox_tiny": ROOT / "scripts" / "examples" / "camera" / "video_tracking_config_tiny.json"}


def run(video: Path, models: list[str], nths: list[int], out: Callable[[str], None]) -> dict:
    import cv2  # noqa: PLC0415

    from camera_adapter import bounded  # noqa: PLC0415
    from camera_adapter import person_video as pv  # noqa: PLC0415
    cap = cv2.VideoCapture(str(video))
    fps, size, n = cap.get(cv2.CAP_PROP_FPS), (int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))), int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    rows, missing = [], []
    for name in models:
        path = ROOT / "models" / f"{name}.onnx"
        if not path.is_file():
            missing.append(name)
            continue
        info = pv.verify_model(path, name)
        cfg = pv.load_config(CONFIGS[name])
        det = pv.YoloxPersonDetector(path, cfg.detector)
        _, log = pv.track_video(video, cfg, detector=det, model_info=info)
        sp = log["speed"]
        rows.append({"model": name, "mode": "offline", "every_nth": 1, "input_size": cfg.detector.input_size, "frames_processed": log["video"]["decoded_frames"],
                     "dropped": 0, "sampled_out": 0, "source_duration_s": sp["source_duration_s"], "processing_duration_s": sp["processing_duration_s"],
                     "frames_per_second": sp["frames_per_second"], "realtime_factor": sp["realtime_factor"], "kept_up": sp["kept_up_with_source_rate"],
                     "processing_lag_s": None, "observations": log["observations"], "tracks": len(log["tracks"]), "detector_ms_per_frame": sp["detector_ms_per_frame"]})
        out(f"  {name} offline: {sp['frames_per_second']} frames/s, realtime factor {sp['realtime_factor']}")
        for nth in nths:
            bcfg = bounded.BoundedConfig(queue_size=2, process_every_nth=nth)
            ft = pv.FrameTracker(cfg, det, fps, size[0], size[1], frame_step=nth)
            res = bounded.run_bounded(bounded.paced_file_source(video), lambda i, t, f: ft.process(i, t, f), bcfg)
            blog = bounded.bounded_log(res, bcfg, fps, f"paced replay of {video.name} (not a camera)")
            fr = blog["frames"]
            rows.append({"model": name, "mode": "paced (bounded)", "every_nth": nth, "input_size": cfg.detector.input_size, "frames_processed": fr["processed"],
                         "dropped": fr["dropped"], "sampled_out": fr["sampled_out"], "source_duration_s": round(res.released / fps, 3), "processing_duration_s": round(res.wall_s, 3),
                         "frames_per_second": round(fr["processed"] / res.wall_s, 3), "realtime_factor": None, "kept_up": blog["kept_up"],
                         "processing_lag_s": blog["processing_lag_s"], "longest_drop_run_s": fr["longest_drop_run_s"], "observations": len(ft.obs), "tracks": len(ft.tracks),
                         "detector_ms_per_frame": ft.summary()["detector_ms_per_frame"]})
            out(f"  {name} paced every {nth}: processed {fr['processed']}, dropped {fr['dropped']}, sampled out {fr['sampled_out']}, lag {blog['processing_lag_s']}, kept up {blog['kept_up']}")
    return {"video": {"file": video.name, "fps": fps, "size": list(size), "frames": n, "sha256": pv.sha256_file(video)}, "rows": rows, "missing_models": missing,
            "machine": {"platform": platform.platform(), "processor": platform.processor(), "python": platform.python_version(), "opencv_threads": cv2.getNumThreads()},
            "versions": pv.package_versions(), "note": "Paced replay of a file is not a camera. Offline throughput says nothing about live capture, whose frames cannot wait."}


def main(argv: list[str] | None = None, *, out: Callable[[str], None] = print) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--video", type=Path)
    p.add_argument("--models", default="yolox_s,yolox_tiny")
    p.add_argument("--every-nth", default="1,2,3")
    p.add_argument("--report", type=Path)
    try:
        args = p.parse_args(argv)
        nths = [int(x) for x in args.every_nth.split(",")]
        models = [m for m in args.models.split(",") if m]
        if any(m not in CONFIGS for m in models) or not all(1 <= n <= 30 for n in nths):
            raise ValueError
    except (SystemExit, ValueError):
        return 2
    try:
        import cv2  # noqa: F401, PLC0415
        import supervision  # noqa: F401, PLC0415
        import trackers  # noqa: F401, PLC0415
    except ImportError as exc:
        out(f"MISSING OPTIONAL DEPENDENCY ({exc.name}): see requirements-video.txt")
        return 2
    with tempfile.TemporaryDirectory(prefix="athletrix-bench-") as tmp:
        video = args.video
        if video is None:
            from camera_adapter import synthetic_people  # noqa: PLC0415
            video = Path(tmp) / "synthetic-people.avi"
            synthetic_people.render(video)
            out("rendered the drawn-figure scene (fictional) as the benchmark input")
        report = run(video, models, nths, out)
    if not report["rows"]:
        out(f"No pinned model file is available ({', '.join(report['missing_models'])}); fetch with scripts/fetch_person_model.py")
        return 1
    report = {"tool": "camera_benchmark", "generated_at": datetime.now(UTC).isoformat(), **report}
    path = args.report or ROOT / "reports" / f"camera-benchmark-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    out(f"BENCHMARK ({report['video']['size'][0]}x{report['video']['size'][1]} at {report['video']['fps']:g} fps, {report['video']['frames']} frames; {report['machine']['platform']})")
    for r in report["rows"]:
        lag = r["processing_lag_s"]
        out(f"  {r['model']:10s} {r['mode']:15s} every {r['every_nth']}: {r['frames_processed']} processed, {r['dropped']} dropped, {r['sampled_out']} sampled out; "
            f"{r['frames_per_second']} frames/s" + (f", realtime factor {r['realtime_factor']}" if r["realtime_factor"] else "") +
            (f", lag median {lag['median']} s p95 {lag['p95']} s" if lag else "") + f"; kept up: {r['kept_up']}; {r['observations']} observations, {r['tracks']} tracks")
    if report["missing_models"]:
        out(f"  not run (model file missing): {', '.join(report['missing_models'])}")
    out(f"JSON report: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
