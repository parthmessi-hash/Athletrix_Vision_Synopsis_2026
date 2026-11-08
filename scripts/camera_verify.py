"""Camera verification report against independent references (camera_adapter/verification.py).

USAGE
    python scripts/camera_verify.py make-synthetic --out DIR     # a SYNTHETIC fixture (known path, a gap, an uncertain
                                                                 # interval, sprints, noise near the threshold)
    python scripts/camera_verify.py report INPUTS.json --out DIR [--max-gap-s 0.5] [--threshold 5.5] [--min-samples 3]
INPUTS.json names its tracking samples file (JSON Lines in the engine's interchange format, for example a recording
export) and its references. Every report repeats the fixture kind ("synthetic" or "approved_local"); nothing here is
evidence about real footage unless the inputs came from an approved local clip with independently measured references.
Exit 0 when the report was written (targets missed are reported, not errors), 2 on invalid input.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.sprints import SprintRule  # noqa: E402
from app.models import TrackingSample  # noqa: E402
from camera_adapter.verification import verify  # noqa: E402

T0 = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


def z(t):
    return t.isoformat().replace("+00:00", "Z")


def make_synthetic(out_dir: Path, out) -> int:
    """SYNTHETIC: one athlete runs a 40 m course at 4 m/s, then two sprints (peak 7 m/s), a stretch hovering around the
    threshold, a 1.5 s tracking gap and an interval with uncertain identity; measurement noise 0.15 m/s."""
    out_dir.mkdir(parents=True)
    rng = random.Random("athletrix-camera-verify")
    rows, t, seg = [], 0.0, "seg-1"

    def add(v, dur, segment=None):
        nonlocal t, seg
        if segment:
            seg = segment
        for _ in range(int(round(dur * 10))):
            t += 0.1
            rows.append(TrackingSample(athlete_id="syn-1", timestamp=T0 + timedelta(seconds=t), x_position=0.0, y_position=0.0, speed_mps=max(0.0, round(v(t) + rng.gauss(0, 0.15), 3)),
                                       acceleration_mps2=0.0, tracking_confidence=0.9, source="camera", segment_id=seg, value_basis="estimated"))
    add(lambda s: 4.0, 10.0)                                   # 0-10 s: course run, 40 m true
    add(lambda s: 2.0, 5.0)                                    # 10-15 s
    add(lambda s: 4.0 + 3.0 * math.sin(math.pi * (s - 15) / 3), 3.0)   # 15-18 s: sprint 1, peak 7 m/s
    add(lambda s: 2.0, 4.0)                                    # 18-22 s
    add(lambda s: 5.4, 3.0)                                    # 22-25 s: hovering just under the threshold (noise)
    add(lambda s: 2.0, 3.0)                                    # 25-28 s
    t += 1.5                                                   # 28-29.5 s: tracking gap
    add(lambda s: 4.0 + 3.0 * math.sin(math.pi * (s - 29.5) / 3), 3.0, "seg-2")   # 29.5-32.5 s: sprint 2, after a re-confirmed track
    add(lambda s: 2.0, 7.5)                                    # 32.5-40 s, of which 35-38 s uncertain identity
    with open(out_dir / "tracking.jsonl", "w") as f:
        for s in rows:
            f.write(json.dumps({"type": "tracking", "sample": s.model_dump(mode="json")}) + "\n")
    at = lambda s: z(T0 + timedelta(seconds=s))  # noqa: E731
    doc = {"format": "athletrix-camera-verification", "format_version": 1, "fixture_kind": "synthetic",
           "note": "SYNTHETIC: generated path with known truth; not footage and not a real camera.", "tracking_file": "tracking.jsonl",
           "course_runs": [{"athlete_id": "syn-1", "start": at(0), "end": at(10), "measured_distance_m": 40.0, "reference": "synthetic truth (stands in for a tape-measured course)"}],
           "speed_references": [{"athlete_id": "syn-1", "start": at(15.0), "end": at(18.0), "distance_m": 4.0 * 3 + 3.0 * 3 * 2 / math.pi, "time_s": 3.0,
                                 "reference": "synthetic truth (stands in for timing gates)"}],
           "uncertain_identity": [{"athlete_id": "syn-1", "start": at(35), "end": at(38)}],
           "event_windows": [{"athlete_id": "syn-1", "start": at(0), "end": at(40)}],
           "manual_events": [{"kind": "sprint", "athlete_id": "syn-1", "t": at(16.5), "labeller": "synthetic truth"},
                             {"kind": "sprint", "athlete_id": "syn-1", "t": at(31.0), "labeller": "synthetic truth"},
                             {"kind": "jump", "athlete_id": "syn-1", "t": at(20.0), "labeller": "synthetic truth"}]}
    (out_dir / "inputs.json").write_text(json.dumps(doc, indent=2) + "\n")
    out(f"wrote a SYNTHETIC verification fixture to {out_dir} ({len(rows)} samples)")
    return 0


def report(r: dict) -> str:
    L = ["# Camera verification report", "", f"**Fixture kind: {r['fixture_kind'].upper()}.** " + ("Synthetic inputs check the software only; they say nothing about a real camera."
                                                                                                  if r["fixture_kind"] == "synthetic" else "Approved local inputs as stated by whoever prepared them."), "",
         f"Targets (from the plan; targets, not results): distance and speed within {r['targets']['distance_pct']:g} %, event counts within {r['targets']['event_count_pct']:g} %.", "",
         "## Course distance", "| Athlete | measured m | engine m | error % | tracked share | excluded | within target |", "|---|---|---|---|---|---|---|"]
    for c in r["course_runs"]:
        L.append(f"| {c['athlete_id']} | {c['measured_distance_m']} | {c['engine']['distance_m']} | {c['error_pct']} | {c['engine']['tracked_share']} | {c['engine']['excluded']} | {c['within_target']} |")
    L += ["", "## Average speed over timed intervals", "| Athlete | reference m/s | engine m/s | error % | tracked share | within target |", "|---|---|---|---|---|---|"]
    for s in r["speed"]:
        L.append(f"| {s['athlete_id']} | {s['reference_average_speed_mps']} | {s['engine_average_speed_mps']} | {s['error_pct']} | {s['tracked_share']} | {s['within_target']} |")
    L += ["A timed sprint gives an average speed over the interval, not an instantaneous top speed.", "", "## Peak speed"]
    L += [f"- {p.get('status') or ''} {p.get('reason') or ''}".strip() if "status" in p else f"- {p['athlete_id']}: reference {p['peak_mps']} m/s, engine {p['engine_peak_mps']} m/s, error {p['error_pct']} %"
          for p in r["peak_speed"]]
    L += ["", "## Events"]
    for e in r["events"]:
        s = e["sprint"]
        L.append(f"- {e['athlete_id']}: sprints manual {s['manual']}, detected {s['detected']}, matched {s['matched']}, missed {s['missed']}, false {len(s['false_detections'])}; "
                 f"count error {s['count_error_pct']} % (within target: {s['within_target']})" + (f" — {s['note']}" if s.get("note") else ""))
        for k in ("jump", "tackle"):
            L.append(f"  - {k}: manual {e[k]['manual']}; automatic {e[k]['automatic']}")
    return "\n".join(L) + "\n"


def main(argv=None, out=print) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("make-synthetic"); m.add_argument("--out", type=Path, required=True)
    r = sub.add_parser("report"); r.add_argument("inputs", type=Path); r.add_argument("--out", type=Path, required=True)
    r.add_argument("--max-gap-s", type=float, default=0.5); r.add_argument("--threshold", type=float, default=5.5); r.add_argument("--min-samples", type=int, default=3)
    args = ap.parse_args(argv)
    if args.out.exists():
        out(f"{args.out} exists; not overwritten")
        return 2
    if args.cmd == "make-synthetic":
        return make_synthetic(args.out, out)
    try:
        doc = json.loads(args.inputs.read_text())
        if doc.get("format") != "athletrix-camera-verification" or doc.get("fixture_kind") not in ("synthetic", "approved_local"):
            raise ValueError("not an athletrix-camera-verification file with fixture_kind synthetic or approved_local")
        samples = [TrackingSample.model_validate(json.loads(line)["sample"]) for line in (args.inputs.parent / doc["tracking_file"]).read_text().splitlines() if line.strip()]
    except (OSError, ValueError, KeyError) as exc:
        out(f"Error: {exc}")
        return 2
    res = verify(doc, samples, args.max_gap_s, SprintRule(threshold_mps=args.threshold, min_samples=args.min_samples))
    args.out.mkdir(parents=True)
    (args.out / "results.json").write_text(json.dumps(res, indent=2, default=str))
    (args.out / "report.md").write_text(report(res))
    out(report(res))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
