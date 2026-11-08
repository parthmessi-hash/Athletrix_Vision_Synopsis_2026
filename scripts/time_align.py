"""Fit, inspect and apply a time alignment between one stream's clock and UTC from operator-supplied evidence
(app/core/alignment.py). Nothing is inferred from timestamps that merely look close.

USAGE
    python scripts/time_align.py fit EVENTS.json --out ALIGN.json [--exclude LABEL ...]
    python scripts/time_align.py show ALIGN.json
    python scripts/time_align.py map ALIGN.json STREAM_SECONDS [STREAM_SECONDS ...]
    python scripts/time_align.py compare ALIGN_A.json ALIGN_B.json --needed-s 0.5
EVENTS.json: {"stream": "camera"|"wearable", "stream_id": "...", "basis": "capture_time_mapping"|"operator_estimate"|"replay_anchor",
              "reference_uncertainty_s": 0.04, "tolerance_s": 0.1,
              "events": [{"label": "kick-off whistle", "stream_time_s": 12.48, "reference_utc": "2026-11-07T10:00:00.000Z", "note": "..."}]}
Template: scripts/examples/session/time_alignment_events_template.json (blank). Exit 0 on success, 2 on invalid input.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.alignment import AlignmentError, cross_stream, describe, fit, load, map_time  # noqa: E402


def main(argv=None, out=print) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fit"); f.add_argument("events", type=Path); f.add_argument("--out", type=Path, required=True); f.add_argument("--exclude", nargs="*", default=[])
    f.add_argument("--no-drift", action="store_true", help="fit an offset only")
    s = sub.add_parser("show"); s.add_argument("alignment", type=Path)
    m = sub.add_parser("map"); m.add_argument("alignment", type=Path); m.add_argument("times", type=float, nargs="+")
    c = sub.add_parser("compare"); c.add_argument("a", type=Path); c.add_argument("b", type=Path); c.add_argument("--needed-s", type=float, required=True)
    args = ap.parse_args(argv)
    try:
        if args.cmd == "fit":
            if args.out.exists():
                out(f"{args.out} exists; not overwritten")
                return 2
            ev = json.loads(args.events.read_text(encoding="utf-8"))
            d = fit(ev["events"], basis=ev["basis"], stream=ev["stream"], stream_id=ev.get("stream_id", ""), reference_uncertainty_s=ev.get("reference_uncertainty_s") or 0.0,
                    tolerance_s=ev.get("tolerance_s", 0.1), allow_drift=not args.no_drift, exclude=args.exclude)
            args.out.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")
            args.alignment = args.out
        if args.cmd in ("fit", "show"):
            d = load(args.alignment)
            out(describe(d))
            for r in d.get("residuals", []):
                out(f"  {r['label']:<28} stream {r['stream_time_s']:>10.3f} s  residual {r['residual_s']:+.4f} s" + ("  POSSIBLY MISMATCHED" if r.get("possibly_mismatched") else ""))
            if d.get("model"):
                out(f"  RMS {d['rms_s']:.4f} s, max {d['max_abs_residual_s']:.4f} s, uncertainty ±{d['uncertainty_s']:.4f} s ({d['uncertainty_basis']})")
            for w in d.get("warnings", []):
                out(f"  warning: {w}")
        elif args.cmd == "map":
            d = load(args.alignment)
            for t in args.times:
                out(json.dumps(map_time(d, t)))
        elif args.cmd == "compare":
            r = cross_stream(load(args.a), load(args.b), args.needed_s)
            out(("AVAILABLE: " if r["available"] else "UNAVAILABLE: ") + "; ".join(r["reasons"]))
            return 0 if r["available"] else 1
    except (AlignmentError, KeyError, ValueError, OSError) as exc:
        out(f"Error: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
