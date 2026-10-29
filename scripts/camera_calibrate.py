"""Fit, save and check a planar field calibration for one fixed camera (file-based; no UI, no network).

USAGE
    python scripts/camera_calibrate.py fit POINTS.json --out CALIBRATION.json      # fit and save (refuses to overwrite)
    python scripts/camera_calibrate.py check CALIBRATION.json                      # re-validate a saved file and summarise

POINTS.json (format "athletrix-calibration-points", version 1):
    {"format": "athletrix-calibration-points", "format_version": 1, "camera_id": "sideline-cam-1",
     "image": {"width": 960, "height": 540},
     "fit_points":   [{"label": "corner A", "image": [u, v], "field": [x, y]}, ...],   # at least 4, no 3 of 4 collinear
     "check_points": [{"label": "penalty spot", "image": [u, v], "field": [x, y]}]}  # optional, never used in the fit
  image: pixels of the full frame, OpenCV convention ((0, 0) = centre of the top-left pixel, u right, v down).
  field: metres on the ground plane, in the axes of your measured points.

The result records the transformation, both point sets, the fitting residual (NOT independent accuracy), check-point
errors in metres when check points are given (the only independent figure), conventions, assumptions (fixed camera,
planar ground, no lens-distortion model) and software identifiers. No pass/fail threshold is applied: decide and record
what error is acceptable for your use. Moving or zooming the camera requires a new calibration.

EXIT CODES: 0 ok · 1 invalid points, unusable transformation or file problem · 2 bad arguments
"""

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from camera_adapter import calibration as cal  # noqa: E402

POINTS_FORMAT = "athletrix-calibration-points"


def summary(c: cal.Calibration, out: Callable[[str], None]) -> None:
    out(f"Camera {c.camera_id or '(unnamed)'} · image {c.width}x{c.height} · {len(c.fit_points)} fitting points · {len(c.check_points)} check points")
    out(f"  fitting residual (not independent accuracy): rms {c.fit_residual_m['rms']:.4f} m, max {c.fit_residual_m['max']:.4f} m")
    if c.check_errors_m:
        out(f"  check points (independent of the fit): rms {c.check_errors_m['rms']:.4f} m, max {c.check_errors_m['max']:.4f} m")
        for p in c.check_errors_m["per_point"]:
            out(f"    {p['label'] or '(unlabelled)'}: {p['error_m']:.4f} m")
    else:
        out("  no check points: there is no independent estimate of this calibration's error")
    out("  assumptions: " + "; ".join(cal.ASSUMPTIONS))


def opencv_version() -> str | None:
    try:
        import cv2  # noqa: PLC0415
        return cv2.__version__
    except Exception:  # noqa: BLE001  optional
        return None


def main(argv: list[str] | None = None, *, out: Callable[[str], None] = print) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    f = sub.add_parser("fit")
    f.add_argument("points", type=Path)
    f.add_argument("--out", type=Path, required=True)
    c = sub.add_parser("check")
    c.add_argument("calibration", type=Path)
    try:
        args = p.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    try:
        if args.command == "check":
            calib = cal.load(args.calibration)
            out(f"VALID CALIBRATION {args.calibration}")
            summary(calib, out)
            return 0
        if args.out.exists():
            out(f"REFUSED: {args.out} already exists; choose another name (calibrations are kept, not overwritten)")
            return 1
        doc = json.loads(args.points.read_text(encoding="utf-8"))
        if not isinstance(doc, dict) or doc.get("format") != POINTS_FORMAT or doc.get("format_version") != 1:
            out(f"INVALID: {args.points} is not an {POINTS_FORMAT} version 1 file")
            return 1
        img = doc.get("image") or {}
        calib = cal.fit(img.get("width"), img.get("height"), doc.get("fit_points") or [], doc.get("check_points") or [], camera_id=str(doc.get("camera_id", "")),
                        software={"tool": "scripts/camera_calibrate.py", "points_file": args.points.name, "opencv": opencv_version()})
    except (cal.CalibrationError, ValueError, OSError) as exc:
        out("INVALID CALIBRATION: nothing was saved.")
        for e in getattr(exc, "errors", [str(exc)]):
            out(f"  - {e}")
        return 1
    calib.save(args.out)
    out(f"SAVED {args.out}")
    summary(calib, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
