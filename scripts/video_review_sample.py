"""Make a synthetic video-review set for the local video review tool (Athletrix_Web tools/video-review.html).

Writes into a NEW folder (nothing is uploaded; nothing is committed):
  synthetic-people.mp4        the drawn-figure scene (camera_adapter/synthetic_people.py: cartoon figures, no footage of
                              any person), re-encoded as H.264 MP4 so a browser can play it
  observations.jsonl          tracked observations (format 3, with near_other_track) from the REAL person detector and
                              tracker on the same frames
  video.json                  {name, sha256, fps, width, height}: identifies the file (not permission, not capture time)

USAGE
    python scripts/video_review_sample.py --out DIR [--model models/yolox_s.onnx]

Needs requirements-video.txt and the model file (scripts/fetch_person_model.py).
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main(argv=None, out=print) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, required=True, help="folder to create (must not exist)")
    ap.add_argument("--model", type=Path, default=ROOT / "models" / "yolox_s.onnx")
    args = ap.parse_args(argv)
    if args.out.exists():
        out(f"{args.out} exists; not overwritten")
        return 2
    if not args.model.is_file():
        out(f"model file {args.model} not found; fetch it with scripts/fetch_person_model.py")
        return 2
    import cv2  # noqa: PLC0415

    from camera_adapter import person_video as pv  # noqa: PLC0415
    from camera_adapter import synthetic_people as sp  # noqa: PLC0415
    from camera_adapter.observations import dump  # noqa: PLC0415
    args.out.mkdir(parents=True)
    avi = args.out / "synthetic-people.avi"
    sp.render(avi, sp.Camera3())
    mp4 = args.out / "synthetic-people.mp4"
    cap, writer = cv2.VideoCapture(str(avi)), cv2.VideoWriter(str(mp4), cv2.VideoWriter_fourcc(*"avc1"), sp.FPS, (sp.WIDTH, sp.HEIGHT))
    if not writer.isOpened():
        out("this OpenCV build cannot write H.264 MP4")
        return 1
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        writer.write(frame)
    cap.release(); writer.release()
    config = pv.VideoTrackingConfig()
    info = pv.verify_model(args.model, config.detector.model)
    obs, log = pv.track_video(mp4, config, detector=pv.YoloxPersonDetector(args.model, config.detector), model_info=info)
    dump(obs, args.out / "observations.jsonl")
    avi.unlink()
    sha = hashlib.sha256(mp4.read_bytes()).hexdigest()
    (args.out / "video.json").write_text(json.dumps({"name": mp4.name, "sha256": sha, "fps": sp.FPS, "width": sp.WIDTH, "height": sp.HEIGHT}, indent=2))
    enc = log.get("encounters", {})
    out(f"wrote {args.out}: {mp4.name} (sha256 {sha[:16]}…), {len(obs.observations)} observations in {len(log['tracks'])} tracks, "
        f"{enc.get('observations_near_other_track', 0)} near another track. Drawn figures only; open them in tools/video-review.html.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
