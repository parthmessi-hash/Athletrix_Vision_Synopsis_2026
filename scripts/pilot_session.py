"""Operator commands for the fatigue pilot's study log (POST/GET /api/v1/recordings/{id}/study).

Run each step as it happens; re-running the same command is safe (its client token is derived from the command, so a
retry is stored once). Start the recording first (scripts/sideline_session.py start PLAN.json, or the dashboard).

USAGE (default engine http://127.0.0.1:8000, default recording: the active one; --engine / --recording to change)
    pilot_session.py session --label day1 --protocol controlled_study|club_demo|rehearsal [--fictional]
                     [--camera-alignment operator_estimate:0.04] [--calibration-sha256 HEX] [--planned-rounds N]
                     [--study-manifest STUDY_MANIFEST.json]
                     # --study-manifest records the manifest's SHA-256 (and the profile it names) in the session with the
                     # engine's own time: the report can then verify the choices were fixed before any outcome. The engine
                     # refuses the session if the manifest names a different profile from the one it would freeze.
    pilot_session.py wearer ATHLETE --code P1 --device VEST_ID --baseline BASELINE_ID --hr-max 200 --hr-max-origin "measured test 2026-11-01"
    pilot_session.py wearer ATHLETE --end
    pilot_session.py ready ATHLETE                       # what the streams allow now, and the next action
    pilot_session.py round-start ATHLETE --round N [--partial "reason"]
                                                         # refused while the streams are not usable; --partial starts it
                                                         # anyway, records the reason for the report, and never opens the
                                                         # camera reference
    pilot_session.py save-score ATHLETE --round N       # 1. saves the score now and prints its confirmation id
    pilot_session.py sprint-start ATHLETE --round N --confirm PREDICTION_ID [--gate-time ISO --gate-offset-s S --gate-uncertainty-s U]
                                                         # 2. only after the confirmation: start the timed sprint
    (sprint-start without --confirm still works, but its prediction is labelled ORDER UNVERIFIED)
    pilot_session.py sprint-end ATHLETE --round N
    pilot_session.py round-end ATHLETE --round N [--invalid "reason"]
    pilot_session.py result ATHLETE --round N (--time 3.21 --method timing_gates | --missing equipment_failure) [--correct ENTRY_ID --why "reason"]
    pilot_session.py rating ATHLETE --round N (--value 6 | --missing not_collected) [--notes "..."]
    pilot_session.py rounds                              # the saved rounds: prediction, timing label, result
    pilot_session.py coverage [--at ISO]                 # the active wearer's reference coverage for the EXPERIMENTAL
                                                         # movement-matched feature: comparable speed bands, lost data, next check
Exit codes: 0 stored (or already stored; for ready: ready to start or save), 1 ready: not ready yet, 2 refused (the
reason is printed), 3 engine unreachable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def token(args, kind: str) -> str:
    parts = {k: v for k, v in vars(args).items() if k not in ("engine", "recording", "cmd") and v not in (None, False)}
    return f"cli-{kind}-" + hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:24]


def readiness_lines(d: dict) -> list[str]:
    L = [f"  state: {d['state'].upper()}" + (f" · round {d['open_round']} open" if d.get("open_round") else "")]
    for k, v in (d.get("streams") or {}).items():
        L.append(f"  {k}: {'usable' if v['usable'] else 'NOT usable'} · {v['detail']}")
    if d.get("estimates_available"):
        L.append("  estimates now: " + " · ".join(f"{k} {'yes' if v else 'no'}" for k, v in d["estimates_available"].items()))
    L.append(f"  NEXT: {d['next']}")
    return L


def main(argv=None, out=print, client: httpx.Client | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--engine", default="http://127.0.0.1:8000")
    ap.add_argument("--recording")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("session"); p.add_argument("--label", required=True); p.add_argument("--protocol", required=True, choices=["controlled_study", "club_demo", "rehearsal"])
    p.add_argument("--fictional", action="store_true"); p.add_argument("--camera-alignment"); p.add_argument("--calibration-sha256"); p.add_argument("--planned-rounds", type=int)
    p.add_argument("--study-manifest", type=Path)
    p = sub.add_parser("wearer"); p.add_argument("athlete"); p.add_argument("--code"); p.add_argument("--device"); p.add_argument("--baseline")
    p.add_argument("--hr-max", type=float); p.add_argument("--hr-max-origin"); p.add_argument("--end", action="store_true")
    for name in ("round-start", "save-score", "sprint-start", "sprint-end", "round-end"):
        p = sub.add_parser(name); p.add_argument("athlete"); p.add_argument("--round", type=int, required=True)
        if name == "round-end":
            p.add_argument("--invalid")
        if name == "round-start":
            p.add_argument("--partial", metavar="REASON")
        if name == "sprint-start":
            p.add_argument("--confirm"); p.add_argument("--gate-time"); p.add_argument("--gate-offset-s", type=float); p.add_argument("--gate-uncertainty-s", type=float)
    p = sub.add_parser("result"); p.add_argument("athlete"); p.add_argument("--round", type=int, required=True); p.add_argument("--time", type=float)
    p.add_argument("--method", choices=["timing_gates", "stopwatch", "video_frames", "other"]); p.add_argument("--missing"); p.add_argument("--correct"); p.add_argument("--why")
    p.add_argument("--by")
    p = sub.add_parser("rating"); p.add_argument("athlete"); p.add_argument("--round", type=int, required=True); p.add_argument("--value", type=float)
    p.add_argument("--missing"); p.add_argument("--notes")
    sub.add_parser("rounds")
    p = sub.add_parser("coverage"); p.add_argument("--at")
    p = sub.add_parser("ready"); p.add_argument("athlete")
    args = ap.parse_args(argv)
    e = args.engine.rstrip("/") + "/api/v1"
    own = client is None
    c = client or httpx.Client(timeout=15.0)
    try:
        rid = args.recording or (c.get(f"{e}/recordings/active").json().get("recording") or {}).get("id")
        if not rid:
            out("No active recording: start one (scripts/sideline_session.py start PLAN.json) or pass --recording.")
            return 2
        if args.cmd == "ready":
            r = c.get(f"{e}/recordings/{rid}/study/readiness", params={"athlete_id": args.athlete})
            if r.status_code != 200:
                out(f"Refused (HTTP {r.status_code}): {r.text[:200]}")
                return 2
            for line in readiness_lines(r.json()):
                out(line)
            return 0 if r.json()["state"] in ("ready_to_start", "ready_to_save") else 1
        if args.cmd == "coverage":
            from app.core.reference_coverage import coverage_lines  # noqa: PLC0415
            r = c.get(f"{e}/recordings/{rid}/study/pilot", params={"trend_span_s": 0, **({"at": args.at} if args.at else {})})
            if r.status_code != 200:
                out(f"Refused (HTTP {r.status_code}): {r.text[:200]}")
                return 2
            d = r.json()
            if not d.get("wearer"):
                out(f"No active wearer at {d.get('at')}: {d.get('reason')}")
                return 1
            for line in coverage_lines(d.get("reference_coverage")):
                out(line)
            return 0
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
                    (f" · timing {r['timing_evidence']['category']} ({r['timing_evidence']['basis']})" if r.get("timing_evidence") else "") +
                    f" · time {res.get('time_s') or res.get('missing_reason') or 'not entered'}" + (" · INVALID" if inv.get("notes") and "invalid" in str(inv) else ""))
            return 0
        k = {"round-start": "round_start", "save-score": "score_save", "sprint-start": "sprint_start", "sprint-end": "sprint_end", "round-end": "round_end", "result": "sprint_result",
             "rating": "self_rating", "session": "session", "wearer": "wearer"}[args.cmd]
        body = {"kind": k, "client_token": token(args, k)}
        if args.cmd == "session":
            refs = []
            if args.camera_alignment:
                basis, _, unc = args.camera_alignment.partition(":")
                refs.append({"stream": "camera", "basis": basis, "uncertainty_s": float(unc) if unc else None})
            body.update(session_label=args.label, protocol=args.protocol, fictional=args.fictional, clock_references=refs, calibration_sha256=args.calibration_sha256,
                        planned_rounds=args.planned_rounds)
            if args.study_manifest:
                raw = args.study_manifest.read_bytes()
                body.update(study_manifest_sha256=hashlib.sha256(raw).hexdigest(), study_manifest_profile_sha256=json.loads(raw)["profile_sha256"])
        elif args.cmd == "wearer":
            body.update(athlete_id=args.athlete, end=args.end) if args.end else body.update(athlete_id=args.athlete, participant_code=args.code, device_id=args.device,
                                                                                          baseline_id=args.baseline, hr_max_bpm=args.hr_max, hr_max_origin=args.hr_max_origin)
        else:
            body.update(athlete_id=args.athlete, round=args.round)
            if args.cmd == "sprint-start" and args.confirm:
                body.update(confirms_prediction_id=args.confirm)
            if args.cmd == "sprint-start" and args.gate_time:
                body.update(start_reference={"source": "timing_gate", "device_time": args.gate_time, "clock_offset_s": args.gate_offset_s, "clock_uncertainty_s": args.gate_uncertainty_s})
            if args.cmd == "round-start" and args.partial:
                body.update(partial_reason=args.partial)
            if args.cmd == "round-end" and args.invalid:
                body.update(invalid=True, notes=args.invalid)
            if args.cmd == "result":
                body.update(time_s=args.time, method=args.method, missing_reason=args.missing, recorded_by=args.by)
                if args.correct:
                    body.update(supersedes=args.correct, correction_reason=args.why)
            if args.cmd == "rating":
                body.update(value=args.value, missing_reason=args.missing, notes=args.notes)
        r = c.post(f"{e}/recordings/{rid}/study", json={k2: v for k2, v in body.items() if v is not None})
        if r.status_code == 409 and isinstance((r.json() or {}).get("detail"), dict) and "readiness" in r.json()["detail"]:
            out("Refused: the round was NOT started.")
            for line in readiness_lines(r.json()["detail"]["readiness"]):
                out(line)
            out(f'  To start anyway, re-run with --partial "reason" (the reason goes into the report; the camera reference is not opened).')
            return 2
        if r.status_code not in (200, 201):
            out(f"Refused (HTTP {r.status_code}): {r.json().get('detail') if r.headers.get('content-type', '').startswith('application/json') else r.text[:200]}")
            return 2
        x = r.json()
        out(("Already stored: " if r.status_code == 200 else "Stored: ") + f"{x['kind']} {x['id']} at {x['at']}")
        if args.cmd == "round-start":
            out("  camera reference: " + {True: "OPENED by this round start", False: "not opened by this start" + (f" (partial: {x.get('partial_reason')})" if x.get("partial_reason") else ""),
                                          None: "not recorded"}[x.get("opens_camera_reference")])
        if args.cmd == "save-score":
            p = x["prediction"]
            cands = (p.get("result") or {}).get("candidates") or {}
            out(f"  SAVED score for {x['athlete_id']} round {x['round']} · cutoff {p['cutoff']} · all three available: {'yes' if p.get('all_three_available') else 'NO'}")
            out("  " + " · ".join(f"{n} {v['value'] if v['available'] else 'not available: ' + v['reasons'][0]}" for n, v in cands.items()))
            out(f"  Now start the timed sprint with: sprint-start {x['athlete_id']} --round {x['round']} --confirm {x['id']}")
        if args.cmd == "sprint-start" and args.confirm:
            out(f"  confirmed saved score {args.confirm}; enter the time separately (scripts/sprint_timing.py)")
        elif args.cmd == "sprint-start":
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
