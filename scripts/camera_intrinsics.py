"""Optional lens-distortion calibration from chessboard images (camera_adapter/intrinsics.py). Needs OpenCV and NumPy.

USAGE
    python scripts/camera_intrinsics.py calibrate IMAGE [IMAGE ...] --inner-corners 9x6 --square-mm 30 --camera-id CAM --out LENS.json
                                          [--held-out-every 4]
    python scripts/camera_intrinsics.py synthetic --out DIR [--views 16]      # rendered views through a KNOWN made-up lens (demonstration only)

calibrate  detects the inner corners in every image (all images must have one resolution), fits the lens model on most
           views and checks it on every n-th view held out, then writes the versioned file. A view whose board is not
           found is listed and skipped. Fit RMS is not accuracy; the held-out RMS checks the lens model only.
synthetic  renders chessboard views through a known lens (k1 = -0.32) so the workflow can be tried; recovering that lens
           says nothing about a physical camera.
Then fit the ground calibration with it: scripts/camera_calibrate.py fit POINTS.json --out CAL.json --intrinsics LENS.json
(the measured ground points stay in raw pixels).
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main(argv=None, out=print) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    c = sub.add_parser("calibrate", help="fit a lens model from chessboard images")
    c.add_argument("images", type=Path, nargs="+")
    c.add_argument("--inner-corners", required=True, help="COLSxROWS of inner corners, e.g. 9x6")
    c.add_argument("--square-mm", type=float, required=True)
    c.add_argument("--camera-id", default="")
    c.add_argument("--held-out-every", type=int, default=4, help="hold out every n-th found view for checking (0 = none)")
    c.add_argument("--out", type=Path, required=True)
    s = sub.add_parser("synthetic", help="render views through a known made-up lens")
    s.add_argument("--out", type=Path, required=True)
    s.add_argument("--views", type=int, default=16)
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    import cv2  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415

    from camera_adapter import intrinsics as intr  # noqa: PLC0415
    if args.out.exists():
        out(f"{args.out} exists; not overwritten")
        return 2
    if args.command == "synthetic":
        k, d, w, h = [[700.0, 0, 480.0], [0, 700.0, 270.0], [0, 0, 1]], [-0.32, 0.12, 0.001, -0.0005, 0.0], 960, 540
        args.out.mkdir(parents=True)
        rng = np.random.default_rng(3)
        n = 0
        while n < args.views:
            rv, tv = rng.uniform([-0.5, -0.5, -0.2], [0.5, 0.5, 0.2]), np.array([rng.uniform(-200, -40), rng.uniform(-130, -30), rng.uniform(450, 750)])
            img = intr.render_view(k, d, w, h, rv, tv)
            if intr.detect_corners(img, 9, 6) is None:
                continue
            cv2.imwrite(str(args.out / f"view-{n:02d}.png"), img)
            n += 1
        (args.out / "TRUTH.json").write_text(json.dumps({"camera_matrix": k, "dist_coeffs": d, "image": {"width": w, "height": h}, "inner_corners": [9, 6], "square_mm": 30.0,
                                                          "note": "made-up lens used to render these views; for demonstration only"}, indent=2))
        out(f"wrote {n} synthetic views and TRUTH.json to {args.out} (made-up lens k1 = -0.32)")
        return 0
    try:
        cols, rows = (int(x) for x in args.inner_corners.lower().split("x"))
    except ValueError:
        out("--inner-corners must look like 9x6")
        return 2
    views, size, skipped = [], None, []
    for p in args.images:
        img = cv2.imread(str(p))
        if img is None:
            skipped.append(f"{p.name}: not readable")
            continue
        if size is None:
            size = (img.shape[1], img.shape[0])
        elif (img.shape[1], img.shape[0]) != size:
            out(f"{p.name} is {img.shape[1]}x{img.shape[0]}, not {size[0]}x{size[1]}: one lens model belongs to one resolution")
            return 1
        found = intr.detect_corners(img, cols, rows)
        if found is None:
            skipped.append(f"{p.name}: board not found")
            continue
        views.append(found)
    try:
        lens = intr.calibrate_from_points(views, size[0], size[1], cols=cols, rows=rows, square_mm=args.square_mm, held_out_every=args.held_out_every,
                                          camera_id=args.camera_id, software={"tool": "scripts/camera_intrinsics.py", "opencv": cv2.__version__, "images": len(args.images), "skipped": skipped})
    except (intr.IntrinsicsError, TypeError) as exc:
        out(f"NOT CALIBRATED: {exc}")
        return 1
    lens.save(args.out)
    out(f"SAVED {args.out}: {lens.fit['views']} fitting views, rms {lens.fit['rms_px']} px (residual, not accuracy); "
        + (f"{lens.held_out['views']} held-out views, rms {lens.held_out['rms_px']} px (checks the lens model only)" if lens.held_out else "no held-out views: no independent check"))
    for sk in skipped:
        out(f"  skipped {sk}")
    out("  no threshold is applied; deciding whether this is good enough is a recorded human decision")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
