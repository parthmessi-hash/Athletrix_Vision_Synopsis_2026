"""Track people in a local video file and write camera observations for the adapter (optional; needs requirements-video.txt).

USAGE
    python scripts/camera_track_video.py VIDEO --model models/yolox_s.onnx --out OBS.jsonl --log TRACK-LOG.json \\
        [--config scripts/examples/camera/video_tracking_config_example.json] [--gallery TRACKS.png] [--annotate REVIEW.mp4] \\
        [--assignments-template ASSIGN.json] [--max-frames N]

then, after reviewing the gallery or annotated video and filling in the assignments you confirmed:
    python scripts/camera_calibrate.py fit POINTS.json --out CAL.json
    python scripts/camera_to_samples.py export OBS.jsonl --calibration CAL.json --config ADAPTER-CONFIG.json \\
        --assignments ASSIGN.json --anchor-utc 2026-10-29T12:00:00Z --out samples.jsonl --log adapter-log.json

WHAT IT DOES
  Runs a pretrained person detector (YOLOX ONNX, pinned SHA-256, cached on disk; nothing is downloaded) and ByteTrack
  (Roboflow trackers) over every decoded frame and writes observations in the adapter's format version 1: frame,
  video-relative time (frame / fps), temporary track id, the box's bottom-centre in image pixels, the detector's score,
  and athlete_id null for every observation. The log records the model and its SHA-256, every setting, package versions,
  the video's SHA-256, decoder timestamp mismatches (dropped or duplicated frames), per-track first/last frames and
  losses, skips by reason, and processing speed.

  --gallery writes one image with crops of every track and --annotate a video with boxes and track ids: an assignment
  aid for the operator, not a verified identity. --assignments-template writes an assignments file with every track
  listed for review and no assignment: unassigned tracks never produce athlete samples.

  The detector finds every visible person (players, referees, coaches, spectators); nothing here reads jerseys,
  re-identifies people or classifies soccer roles. Use only footage you have permission to process; generated files
  belong in an ignored folder (camera-work/), never in a commit.

EXIT CODES: 0 ok · 1 invalid input, missing dependency or model, or a failed run · 2 bad arguments
"""

import argparse
import json
import shlex
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from camera_adapter.observations import dump, load  # noqa: E402


def main(argv: list[str] | None = None, *, out: Callable[[str], None] = print, detector=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("video", type=Path)
    p.add_argument("--model", type=Path, help="local ONNX model file (fetch once with scripts/fetch_person_model.py)")
    p.add_argument("--model-name", default=None, help="name of the pinned model (default: from the configuration)")
    p.add_argument("--allow-unpinned-model", action="store_true")
    p.add_argument("--config", type=Path)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--log", type=Path, required=True)
    p.add_argument("--gallery", type=Path)
    p.add_argument("--annotate", type=Path)
    p.add_argument("--assignments-template", type=Path)
    p.add_argument("--max-frames", type=int)
    try:
        args = p.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    try:
        from camera_adapter import person_video as pv  # noqa: PLC0415
        import cv2  # noqa: F401, PLC0415
        import supervision  # noqa: F401, PLC0415
        import trackers  # noqa: F401, PLC0415
    except ImportError as exc:
        out(f"MISSING OPTIONAL DEPENDENCY ({exc.name}): see requirements-video.txt for the two install commands. The engine itself does not need these.")
        return 1
    for path in (args.out, args.log, args.gallery, args.annotate, args.assignments_template):
        if path is not None and path.exists():
            out(f"REFUSED: {path} already exists; nothing was written")
            return 1
    if not args.video.is_file():
        out(f"INVALID INPUT: {args.video} is not a file")
        return 1
    try:
        config = pv.load_config(args.config)
        if detector is None:
            if args.model is None:
                out("REFUSED: --model is required (a local ONNX file; processing never downloads one)")
                return 2
            name = args.model_name or config.detector.model
            model_info = pv.verify_model(args.model, name, allow_unpinned=args.allow_unpinned_model)
            if name in pv.MODELS and pv.MODELS[name]["input_size"] != config.detector.input_size:
                raise ValueError(f"detector.input_size {config.detector.input_size} does not match {name}'s export ({pv.MODELS[name]['input_size']})")
            detector = pv.YoloxPersonDetector(args.model, config.detector)
        else:
            model_info = {"name": "test fixture detector", "sha256": None, "pinned": False}
        import cv2 as _cv2  # noqa: PLC0415
        cap = _cv2.VideoCapture(str(args.video))
        fps, size = cap.get(_cv2.CAP_PROP_FPS), (int(cap.get(_cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(_cv2.CAP_PROP_FRAME_HEIGHT)))
        cap.release()
        preview = pv.PreviewWriter(args.annotate, args.gallery, fps or 25.0, size) if (args.annotate or args.gallery) else None
        try:
            obs, log = pv.track_video(args.video, config, detector=detector, model_info=model_info, max_frames=args.max_frames, on_frame=preview,
                                      progress=lambda n, total: out(f"  {n} / {total if total > 0 else '?'} frames"))
        finally:
            if preview is not None:
                preview.close()
    except (ValueError, FileNotFoundError, OSError, RuntimeError, TypeError, json.JSONDecodeError) as exc:
        out(f"FAILED: {exc}")
        return 1
    log["command"] = "python scripts/camera_track_video.py " + " ".join(shlex.quote(a) for a in (argv if argv is not None else sys.argv[1:]))
    dump(obs, args.out)
    load(args.out)   # the written file must pass the adapter's own strict validation
    args.log.write_text(json.dumps(log, indent=2) + "\n", encoding="utf-8")
    if args.assignments_template:
        args.assignments_template.write_text(json.dumps(pv.assignment_template(log), indent=2) + "\n", encoding="utf-8")
    sp, v = log["speed"], log["video"]
    out(f"{v['decoded_frames']} frames ({v['width']}x{v['height']} at {v['fps']:g} fps) -> {log['detections']} person detections, {len(log['tracks'])} temporary tracks, "
        f"{log['observations']} observations; skipped {log['skipped']}; track losses {log['track_losses']}; decoder time mismatches {v['decoder_time_mismatches']}")
    out(f"  speed: {sp['frames_per_second']} frames/s (detector {sp['detector_ms_per_frame']} ms, tracker {sp['tracker_ms_per_frame']} ms per frame; {sp['realtime_factor']}x real time)")
    out(f"  model: {model_info.get('name')} sha256 {str(model_info.get('sha256'))[:12]}… pinned={model_info.get('pinned')}")
    out(f"wrote {args.out} and {args.log}" + "".join(f", {x}" for x in (args.gallery, args.annotate, args.assignments_template) if x))
    out("Every observation is unassigned. Assign only tracks you have confirmed (the gallery and annotated video are an aid, not identity).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
