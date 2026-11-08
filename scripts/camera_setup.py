"""Physical camera setup, operator side: the capture checklist, a readable calibration summary, and a compatibility
check against the selected clip or camera configuration (camera_adapter/setup.py). Reads files; changes nothing.

USAGE
    python scripts/camera_setup.py checklist
    python scripts/camera_setup.py inspect CALIBRATION.json [--json]
    python scripts/camera_setup.py compatible CALIBRATION.json --width W --height H [--camera-id ID] [--settings SETTINGS.json]
SETTINGS.json: the camera settings as they are NOW, e.g. {"camera_id": "cam-1", "zoom": "wide end", "focus": "manual 10 m", "mount": "tripod A, 4 m"}.
Exit codes: 0 compatible (warnings allowed), 1 not compatible (a blocking difference), 2 invalid input.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from camera_adapter.setup import CAPTURE_CHECKLIST, compatibility, inspect_calibration  # noqa: E402


def main(argv=None, out=print) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("checklist")
    i = sub.add_parser("inspect"); i.add_argument("calibration", type=Path); i.add_argument("--json", action="store_true")
    c = sub.add_parser("compatible"); c.add_argument("calibration", type=Path); c.add_argument("--width", type=int); c.add_argument("--height", type=int)
    c.add_argument("--camera-id"); c.add_argument("--settings", type=Path)
    args = ap.parse_args(argv)
    if args.cmd == "checklist":
        for n, line in enumerate(CAPTURE_CHECKLIST, 1):
            out(f"{n}. {line}")
        return 0
    s = inspect_calibration(args.calibration)
    if args.cmd == "inspect":
        if args.json:
            out(json.dumps(s, indent=2))
        elif not s["valid"]:
            out("INVALID: " + "; ".join(s["errors"]))
        else:
            out(f"{s['path']} (sha256 {s['sha256'][:12]}) · format {s['format_version']} · camera {s['camera_id'] or 'not stated'} · {s['resolution'][0]}x{s['resolution'][1]} · created {s['created_at']}")
            out(f"  lens model: " + ("none (distortion not corrected)" if not s["lens_model"] else f"yes, fit {s['lens_model']['fit_rms_px']} px, held-out {s['lens_model']['held_out_rms_px']} px"))
            out(f"  camera settings recorded: {json.dumps(s['camera_settings']) if s['camera_settings'] else 'none'}")
            out(f"  fit residual (NOT accuracy): RMS {s['fit_residual_m']['rms']} m, max {s['fit_residual_m']['max']} m over {s['fit_points']} fitting points")
            out("  independent check points: " + (f"RMS {s['check_error_m']['rms']} m, max {s['check_error_m']['max']} m over {s['check_error_m']['points']} points"
                                                   if s["check_error_m"] else "none: accuracy unknown"))
            out("  untested: " + "; ".join(s["untested"]))
        return 0 if s["valid"] else 2
    try:
        settings = json.loads(args.settings.read_text(encoding="utf-8")) if args.settings else None
    except (OSError, ValueError) as exc:
        out(f"Error: {exc}")
        return 2
    res = compatibility(s, {"width": args.width, "height": args.height, "camera_id": args.camera_id, "camera_settings": settings})
    for r in res:
        out(f"[{r['level']}] {r['text']}")
    blocked = any(r["level"] == "block" for r in res)
    out("NOT COMPATIBLE" if blocked else "compatible (see warnings)" if any(r["level"] == "warn" for r in res) else "compatible")
    return 1 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
