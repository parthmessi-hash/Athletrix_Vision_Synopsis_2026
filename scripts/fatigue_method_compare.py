"""Compare the current heart-rate-vs-movement feature (hr_per_speed) with the EXPERIMENTAL candidate (hr_speed_matched)
on FICTIONAL cases. Fictional cases can expose bad behaviour; they cannot establish accuracy on athletes.

USAGE
    .venv/bin/python scripts/fatigue_method_compare.py [--out DIR]

THE FICTIONAL PLAYER: resting HR 60 bpm, HR max 200 bpm (reserve 140 bpm). Heart rate follows movement with a lag:
it approaches 70 + 22 x speed (bpm, speed in m/s) with a 25 s time constant, plus a slow DRIFT (extra bpm at the same
speed) in the fatigue-like cases. Camera speed is sampled at 10 Hz, heart rate at 1 Hz. The reference period is the
first 120 s after the round start; the recent window ends at the cutoff, 420 s after the round start.

CASES
  harder_work          recent movement faster than the reference, no drift: higher HR because of harder work
  same_work_drift      same movement, +12 bpm drift: higher HR at comparable workload (the fatigue-like case)
  slower_drill_no_ref  recent drill much slower, no drift; the reference has no slow movement
  slower_drill_ref     same slow drill, no drift; the reference includes some slow movement
  stop_start           alternating 5 s at 6 m/s and 10 s at 1 m/s, no drift; reference is steady jogging
  clock_uncertain      same_work_drift, but the stated camera clock uncertainty is 2 s
  clock_offset_hidden  same_work_drift, but the camera clock is really 20 s off while 0.04 s is stated
  camera_missing       same_work_drift, no camera samples in the recent window
CHALLENGE CASES (no drift: anything raised is the method misleading, not fatigue)
  stop_start_short     2 s at 5.0 m/s, 4 s at 1.5 m/s (same 2.67 m/s average as the steady reference)
  hr_lingers           running at 3.4 m/s, then an abrupt slowdown to 1.0 m/s for the last 45 s of the recent window
  sharp_change         slow 1.0 m/s, then a step to 3.4 m/s for the last 60 s (the control)
  sharp_change_offset_5s  the same with the camera clock really 5 s off while 0.04 s is stated (hidden)
  sharp_change_uncertain  the same with a stated clock uncertainty of 2 s
Each is scored with the FICTIONAL development profile (combined uses hr_per_speed) and the EXPERIMENTAL profile
(combined uses hr_speed_matched); everything else is identical.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.fatigue import ScoreInputs, Wearer, parse_profile, score  # noqa: E402
from app.models import PhysiologicalSample, TrackingSample  # noqa: E402

T0 = datetime(2026, 11, 9, 10, 0, tzinfo=UTC)
REST, HRMAX = 60.0, 200.0
OLD = ROOT / "scripts/examples/pilot/fictional_fatigue_profile.json"
NEW = ROOT / "scripts/examples/pilot/experimental_matched_profile.json"


def steady(v):
    return lambda t: v


def stop_start(t):
    return 6.0 if (t % 15) < 5 else 1.0


def stop_start_short(t):   # 2 s at 5.0 m/s, 4 s at 1.5 m/s: the same 2.67 m/s average, starting and stopping every 6 s
    return 5.0 if (t % 6) < 2 else 1.5


def slows_at(t_slow, fast, slow):   # hard running, then an abrupt slowdown near the end of the recent window
    return lambda t: fast if t < t_slow else slow


def mixed_ref(t):          # the reference period: jogging and running, optionally with slow stretches
    return 2.4 if (t % 40) < 20 else 3.4


def mixed_ref_with_slow(t):
    return (1.0, 2.4, 3.4)[int(t // 20) % 3]


def simulate(speed_ref, speed_recent, drift_bpm=0.0, cam_offset_s=0.0, camera_recent=True):
    """Seconds 0-120 reference (round start at 0), 120-300 easy jogging, 300-420 the recent drill."""
    def speed(t):
        if t < 120:
            return speed_ref(t)
        if t < 300:
            return 2.5
        return speed_recent(t)
    phys, track = [], []
    hr, tau = 110.0, 25.0
    dt = 0.1
    for k in range(int(420 / dt) + 1):
        t = k * dt
        v = speed(t)
        drift = drift_bpm * min(1.0, max(0.0, (t - 240) / 60))   # the drift builds between 240 and 300 s and stays
        target = 70 + 22 * v + drift
        hr += (target - hr) * dt / tau
        ts = T0 + timedelta(seconds=t)
        if k % 10 == 0:
            phys.append((PhysiologicalSample(athlete_id="fx1", timestamp=ts, source="simulated", device_id="vest", heart_rate_bpm=round(hr, 1)), None))
        if camera_recent or t < 300:
            # the camera clock: a sample of true time t is stamped t + cam_offset_s
            track.append((TrackingSample(athlete_id="fx1", timestamp=ts + timedelta(seconds=cam_offset_s), x_position=0.0, y_position=0.0,
                                         speed_mps=v, acceleration_mps2=0.0, tracking_confidence=0.9, source="simulated",
                                         segment_id="seg-fx1", value_basis="estimated"), None))
    return phys, track


CASES = {
    "harder_work": dict(speed_ref=mixed_ref, speed_recent=steady(4.0)),
    "same_work_drift": dict(speed_ref=mixed_ref, speed_recent=mixed_ref, drift_bpm=12.0),
    "slower_drill_no_ref": dict(speed_ref=mixed_ref, speed_recent=steady(1.0)),
    "slower_drill_ref": dict(speed_ref=mixed_ref_with_slow, speed_recent=steady(1.0)),
    "stop_start": dict(speed_ref=steady(2.67), speed_recent=stop_start),
    "clock_uncertain": dict(speed_ref=mixed_ref, speed_recent=mixed_ref, drift_bpm=12.0, alignment_unc=2.0),
    "clock_offset_hidden": dict(speed_ref=mixed_ref, speed_recent=mixed_ref, drift_bpm=12.0, cam_offset_s=20.0),
    "camera_missing": dict(speed_ref=mixed_ref, speed_recent=mixed_ref, drift_bpm=12.0, camera_recent=False),
    # challenge cases (added 2026-11-09 to look for where the experimental method misleads; no drift in any of them)
    "stop_start_short": dict(speed_ref=steady(2.67), speed_recent=stop_start_short),
    "hr_lingers": dict(speed_ref=mixed_ref_with_slow, speed_recent=slows_at(375, 3.4, 1.0)),
    "sharp_change": dict(speed_ref=mixed_ref_with_slow, speed_recent=slows_at(360, 1.0, 3.4)),
    "sharp_change_offset_5s": dict(speed_ref=mixed_ref_with_slow, speed_recent=slows_at(360, 1.0, 3.4), cam_offset_s=5.0),
    "sharp_change_uncertain": dict(speed_ref=mixed_ref_with_slow, speed_recent=slows_at(360, 1.0, 3.4), alignment_unc=2.0),
}
EXPECTED = {   # what a sensible heart-rate-vs-movement feature should do (written before running the comparison)
    "harder_work": "low, or not enough data if the reference never reached that speed",
    "same_work_drift": "clearly raised: higher heart rate at the same movement",
    "slower_drill_no_ref": "not enough data: the reference has no slow movement to compare with",
    "slower_drill_ref": "low: slow movement compared with slow movement",
    "stop_start": "low: the average movement is the same as the reference jogging",
    "clock_uncertain": "not enough data: the clocks cannot be paired",
    "clock_offset_hidden": "close to same_work_drift (the drift is real); the offset itself cannot be detected from the data",
    "camera_missing": "not enough data (and the combined estimate unavailable, never vest-only under the combined label)",
    "stop_start_short": "low: same average speed as the steady reference, no drift (this simulator's heart rate has no extra cost for accelerations)",
    "hr_lingers": "low: heart rate is still high from the running just before the slowdown, which is lag, not fatigue",
    "sharp_change": "low: a step from slow to hard running, no drift (the control for the next two cases)",
    "sharp_change_offset_5s": "close to sharp_change; the hidden 5 s offset cannot be detected from the two signals",
    "sharp_change_uncertain": "not enough data: the stated clock uncertainty (2 s) is above the tolerance",
}


def run_case(name, spec, prof_path):
    p, sha = parse_profile(prof_path.read_text())
    phys, track = simulate(spec["speed_ref"], spec["speed_recent"], spec.get("drift_bpm", 0.0), spec.get("cam_offset_s", 0.0), spec.get("camera_recent", True))
    base = {"id": "bl-fictional", "rest_hr_bpm": REST, "n": 150, "status": "accepted"}
    w = Wearer(T0 - timedelta(seconds=60), None, "vest", "F1", base, HRMAX, "FICTIONAL")
    cutoff = T0 + timedelta(seconds=420)
    inp = ScoreInputs("fx1", cutoff, "as_entered", phys, track, [w], [], T0, {"stream": "camera", "basis": "operator_estimate", "uncertainty_s": spec.get("alignment_unc", 0.04)})
    r = score(inp, p, sha)
    feat = "hr_speed_matched" if "hr_speed_matched" in p["candidates"]["combined"]["weights"] else "hr_per_speed"
    f = r["features"][feat]
    c = r["candidates"]["combined"]
    pr = f.get("pairs") or {}
    if feat == "hr_speed_matched":
        cov = (f"{f['matched_share_of_recent']:.0%} of {pr.get('recent', 0)} recent pairs in comparable bands" if "matched_share_of_recent" in f else "not paired")
    else:
        cov = f"{pr.get('recent', 0)} recent / {pr.get('reference', 0)} reference pairs" if pr else "not paired"
    return {"feature": feat, "feature_value": f.get("value"), "feature_reason": None if f.get("available") else f.get("reason"), "coverage": cov,
            "measure": f.get("measure"), "combined": c["value"], "combined_reason": None if c["available"] else c["reasons"][0],
            "vest_only": r["candidates"]["vest_only"]["value"], "camera_only": r["candidates"]["camera_only"]["value"]}


def main(argv=None, out=print) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)
    rows = []
    for name, spec in CASES.items():
        rows.append({"case": name, "expected": EXPECTED[name], "current": run_case(name, spec, OLD), "experimental": run_case(name, spec, NEW)})
    L = ["# Heart rate against movement: current vs EXPERIMENTAL (FICTIONAL cases)", "",
         "| case | what should happen | current hr_per_speed | usable (current) | current combined | experimental hr_speed_matched | usable (experimental) | experimental combined |",
         "|---|---|---|---|---|---|---|---|"]
    fmt = lambda x, why: (f"{x:g}" if x is not None else "not available") + (f" ({why[:90]})" if why else "")  # noqa: E731
    for r in rows:
        o, n = r["current"], r["experimental"]
        L.append(f"| {r['case']} | {r['expected']} | {fmt(o['feature_value'], o['feature_reason'])} | {o['coverage']} | {fmt(o['combined'], o['combined_reason'])} | "
                 f"{fmt(n['feature_value'], n['feature_reason'])} | {n['coverage']} | {fmt(n['combined'], n['combined_reason'])} |")
    L += ["", "Fictional cases only: they show behaviour, not accuracy on athletes."]
    for line in L:
        out(line)
    if a.out:
        a.out.mkdir(parents=True, exist_ok=True)
        (a.out / "compare.json").write_text(json.dumps(rows, indent=1, default=str))
        (a.out / "compare.md").write_text("\n".join(L) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
