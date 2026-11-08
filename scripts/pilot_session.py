"""Operator commands for the fatigue pilot's study log (POST/GET /api/v1/recordings/{id}/study).

Run each step as it happens; re-running the same command is safe (its client token is derived from the command, so a
retry is stored once). Start the recording first (scripts/sideline_session.py start PLAN.json, or the dashboard).

USAGE (default engine http://127.0.0.1:8000, default recording: the active one; --engine / --recording to change)
    pilot_session.py session --label day1 --protocol controlled_study|club_demo|rehearsal [--fictional]
                     [--camera-alignment operator_estimate:0.04] [--calibration-sha256 HEX] [--planned-rounds N]
    pilot_session.py wearer ATHLETE --code P1 --device VEST_ID --baseline BASELINE_ID --hr-max 200 --hr-max-origin "measured test 2026-11-01"
    pilot_session.py wearer ATHLETE --end
    pilot_session.py round-start ATHLETE --round N
    pilot_session.py sprint-start ATHLETE --round N      # saves the pre-sprint prediction; press it BEFORE the sprint
    pilot_session.py sprint-end ATHLETE --round N
    pilot_session.py round-end ATHLETE --round N [--invalid "reason"]
    pilot_session.py result ATHLETE --round N (--time 3.21 --method timing_gates | --missing equipment_failure) [--correct ENTRY_ID --why "reason"]
    pilot_session.py rating ATHLETE --round N (--value 6 | --missing not_collected) [--notes "..."]
    pilot_session.py rounds                              # the saved rounds: prediction, timing label, result
Exit codes: 0 stored (or already stored), 2 refused (the reason is printed), 3 engine unreachable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys

import httpx


def token(args, kind: str) -> str:
    parts = {k: v for k, v in vars(args).items() if k not in ("engine", "recording", "cmd") and v not in (None, False)}
    return f"cli-{kind}-" + hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:24]


def main(argv=None, out=print, client: httpx.Client | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--engine", default="http://127.0.0.1:8000")
    ap.add_argument("--recording")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("session"); p.add_argument("--label", required=True); p.add_argument("--protocol", required=True, choices=["controlled_study", "club_demo", "rehearsal"])
    p.add_argument("--fictional", action="store_true"); p.add_argument("--camera-alignment"); p.add_argument("--calibration-sha256"); p.add_argument("--planned-rounds", type=int)
    p = sub.add_parser("wearer"); p.add_argument("athlete"); p.add_argument("--code"); p.add_argument("--device"); p.add_argument("--baseline")
    p.add_argument("--hr-max", type=float); p.add_argument("--hr-max-origin"); p.add_argument("--end", action="store_true")
    for name in ("round-start", "sprint-start", "sprint-end", "round-end"):
        p = sub.add_parser(name); p.add_argument("athlete"); p.add_argument("--round", type=int, required=True)
        if name == "round-end":
            p.add_argument("--invalid")
    p = sub.add_parser("result"); p.add_argument("athlete"); p.add_argument("--round", type=int, required=True); p.add_argument("--time", type=float)
    p.add_argument("--method", choices=["timing_gates", "stopwatch", "video_frames", "other"]); p.add_argument("--missing"); p.add_argument("--correct"); p.add_argument("--why")
    p.add_argument("--by")
    p = sub.add_parser("rating"); p.add_argument("athlete"); p.add_argument("--round", type=int, required=True); p.add_argument("--value", type=float)
    p.add_argument("--missing"); p.add_argument("--notes")
    sub.add_parser("rounds")
    args = ap.parse_args(argv)
    e = args.engine.rstrip("/") + "/api/v1"
    own = client is None
    c = client or httpx.Client(timeout=15.0)
    try:
        rid = args.recording or (c.get(f"{e}/recordings/active").json().get("recording") or {}).get("id")
        if not rid:
            out("No active recording: start one (scripts/sideline_session.py start PLAN.json) or pass --recording.")
            return 2
        if args.cmd == "rounds":
            d = c.get(f"{e}/recordings/{rid}/study").json()
            out(f"Recording {rid} · session {(d['session'] or {}).get('session_label', 'none')} · wearers: " +
                ", ".join(f"{w['participant_code']} {w['start'][11:19]}-{(w['end'] or 'now')[11:19]}" for w in d["wearers"]))
            for r in d["rounds"]:
                p = (r.get("prediction") or {}).get("prediction") or {}
                cands = (p.get("result") or {}).get("candidates") or {}
                res = r.get("sprint_result") or {}
                inv = (r.get("round_end") or {})
                out(f"  {r['athlete_id']} round {r['round']}: " + (" · ".join(f"{k} {v['value'] if v['available'] else 'n/a'}" for k, v in cands.items()) or "no prediction") +
                    (f" · {'prospective' if p.get('prospective', True) else 'RETROSPECTIVE'}" if p else "") +
                    f" · time {res.get('time_s') or res.get('missing_reason') or 'not entered'}" + (" · INVALID" if inv.get("notes") and "invalid" in str(inv) else ""))
            return 0
        k = {"round-start": "round_start", "sprint-start": "sprint_start", "sprint-end": "sprint_end", "round-end": "round_end", "result": "sprint_result",
             "rating": "self_rating", "session": "session", "wearer": "wearer"}[args.cmd]
        body = {"kind": k, "client_token": token(args, k)}
        if args.cmd == "session":
            refs = []
            if args.camera_alignment:
                basis, _, unc = args.camera_alignment.partition(":")
                refs.append({"stream": "camera", "basis": basis, "uncertainty_s": float(unc) if unc else None})
            body.update(session_label=args.label, protocol=args.protocol, fictional=args.fictional, clock_references=refs, calibration_sha256=args.calibration_sha256,
                        planned_rounds=args.planned_rounds)
        elif args.cmd == "wearer":
            body.update(athlete_id=args.athlete, end=args.end) if args.end else body.update(athlete_id=args.athlete, participant_code=args.code, device_id=args.device,
                                                                                          baseline_id=args.baseline, hr_max_bpm=args.hr_max, hr_max_origin=args.hr_max_origin)
        else:
            body.update(athlete_id=args.athlete, round=args.round)
            if args.cmd == "round-end" and args.invalid:
                body.update(invalid=True, notes=args.invalid)
            if args.cmd == "result":
                body.update(time_s=args.time, method=args.method, missing_reason=args.missing, recorded_by=args.by)
                if args.correct:
                    body.update(supersedes=args.correct, correction_reason=args.why)
            if args.cmd == "rating":
                body.update(value=args.value, missing_reason=args.missing, notes=args.notes)
        r = c.post(f"{e}/recordings/{rid}/study", json={k2: v for k2, v in body.items() if v is not None})
        if r.status_code not in (200, 201):
            out(f"Refused (HTTP {r.status_code}): {r.json().get('detail') if r.headers.get('content-type', '').startswith('application/json') else r.text[:200]}")
            return 2
        x = r.json()
        out(("Already stored: " if r.status_code == 200 else "Stored: ") + f"{x['kind']} {x['id']} at {x['at']}")
        if args.cmd == "sprint-start":
            d = c.get(f"{e}/recordings/{rid}/study").json()
            p = next((y["prediction"] for y in d["entries"] if y["kind"] == "prediction" and y["prediction"].get("sprint_start_id") == x["id"]), None)
            if p:
                cands = (p.get("result") or {}).get("candidates") or {}
                out(f"  saved prediction ({p.get('timing_label')}): " + " · ".join(f"{n} {v['value'] if v['available'] else 'not available: ' + v['reasons'][0]}" for n, v in cands.items()))
        return 0
    except httpx.HTTPError as exc:
        out(f"The engine at {args.engine} is not reachable ({exc}); nothing was stored.")
        return 3
    finally:
        if own:
            c.close()


if __name__ == "__main__":
    sys.exit(main())
