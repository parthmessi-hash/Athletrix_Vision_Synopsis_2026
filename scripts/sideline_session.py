"""Local sideline session: plan -> preflight -> start -> status -> stop -> inspect, against one engine.

USAGE
    python scripts/sideline_session.py preflight PLAN.json [--engine URL] [--json]
    python scripts/sideline_session.py start PLAN.json [--engine URL]
    python scripts/sideline_session.py status [--engine URL] [--watch SECONDS] [--count N]
    python scripts/sideline_session.py stop [--engine URL] [--recording ID]
    python scripts/sideline_session.py inspect RECORDING_ID [--engine URL]
Default engine: http://127.0.0.1:8000. Templates: scripts/examples/session/ (session_plan_template.json is blank;
fictional_session_plan.json is a FICTIONAL example).

WHAT EACH STEP DOES (nothing else)
  preflight  engine-side readiness (POST /api/v1/preflight) plus local files (calibration, lens model, time mapping:
             camera_adapter/setup.py), reported separately for raw recording, camera review and the experimental
             recommendation. Exit 0 when raw recording is not blocked, 2 when it is, 3 when the engine is unreachable.
  start      refuses when raw recording is blocked; registers missing athletes (an existing registration is never
             changed), starts ONE recording whose start metadata carries the plan's name, SHA-256 and selected sources,
             and enters each athlete's roster state with the plan's `inputs` and one team constraint entry. Starting the
             same plan again while its recording is active resumes it: entries already made are not repeated.
  status     the active recording's per-player evidence (GET /recordings/{id}/evidence): what is arriving and what is
             missing. It never fills a missing input.
  stop       stops the active (or named) recording; stopping again reports that it is already stopped.
  inspect    a stopped or active recording: per-athlete recorded counts and receipt coverage, roster entries,
             performance entries, saved reviews, responses and the plan metadata.
State lives in the engine; the tool keeps none, so it can be re-run after a crash, a refresh or a restart.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models.session import SessionPlan  # noqa: E402

DEFAULT_ENGINE = "http://127.0.0.1:8000"
VERDICT = {"ready": "READY", "ready_with_warnings": "READY (with warnings)", "blocked": "BLOCKED"}
AREAS = (("raw_recording", "Raw recording"), ("camera_review", "Camera tracking review"), ("experimental_recommendation", "Experimental recommendation"))


class EngineDown(Exception):
    pass


def load_plan(path: Path) -> SessionPlan:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    doc = {k: v for k, v in doc.items() if not k.startswith("_")}       # instructions in templates
    return SessionPlan.model_validate(doc)


def plan_sha(plan: SessionPlan) -> str:
    return hashlib.sha256(plan.model_dump_json().encode()).hexdigest()


def call(client: httpx.Client, method: str, url: str, **kw) -> httpx.Response:
    try:
        return client.request(method, url, **kw)
    except httpx.HTTPError as exc:
        raise EngineDown(f"{type(exc).__name__}: {exc}") from exc


def preflight(client, engine: str, plan: SessionPlan) -> dict:
    r = call(client, "POST", f"{engine}/api/v1/preflight", json=plan.model_dump(mode="json"))
    r.raise_for_status()
    out = r.json()
    from camera_adapter.setup import camera_checks  # noqa: PLC0415
    local = camera_checks(plan.sources.camera.model_dump(mode="json")) if plan.sources.camera.selected else []
    cam = out["readiness"]["camera_review"]
    cam["checks"] = [c for c in cam["checks"] if "run scripts/sideline_session.py" not in c["text"]] + local
    cam["verdict"] = "blocked" if any(c["level"] == "block" for c in cam["checks"]) else ("ready_with_warnings" if any(c["level"] == "warn" for c in cam["checks"]) else "ready")
    return out


def show(out, pf: dict) -> None:
    out(f"Plan {pf['plan']['name']!r} (sha256 {pf['plan']['sha256'][:12]}){' FICTIONAL' if pf['plan']['fictional'] else ''} · engine {pf['engine']['version']} · checked {pf['checked_at']}")
    for key, label in AREAS:
        r = pf["readiness"][key]
        out(f"  {label}: {VERDICT[r['verdict']]}")
        for c in r["checks"]:
            out(f"    [{c['level']}] {c['text']}")


def cmd_preflight(args, client, out) -> int:
    plan = load_plan(args.plan)
    pf = preflight(client, args.engine, plan)
    out(json.dumps(pf, indent=2)) if args.json else show(out, pf)
    return 2 if pf["readiness"]["raw_recording"]["verdict"] == "blocked" else 0


def cmd_start(args, client, out) -> int:
    plan = load_plan(args.plan)
    sha = plan_sha(plan)
    pf = preflight(client, args.engine, plan)
    show(out, pf)
    active = pf["active_recording"]
    if pf["readiness"]["raw_recording"]["verdict"] == "blocked":
        out("Not started: raw recording is blocked (see above). Nothing was registered or started.")
        return 2
    for a in plan.athletes:
        r = call(client, "POST", f"{args.engine}/api/v1/athletes", json={"id": a.id, "name": a.name, "jersey_number": a.jersey_number, "sport": plan.sport, "team_id": plan.team_id})
        if r.status_code not in (201, 409):
            out(f"Not started: registering {a.id} failed (HTTP {r.status_code}: {r.text[:200]})")
            return 2
    if active and active["is_this_plan"]:
        rid = active["id"]
        out(f"Resuming: this plan's recording {rid} is already active.")
    else:
        sources = ",".join(k for k, v in (("wearable", plan.sources.wearable.selected), ("camera", plan.sources.camera.selected)) if v) or "none"
        body = {"name": plan.name, "athlete_ids": [a.id for a in plan.athletes],
                "adapter_configuration": {"note": f"session plan {plan.name}" + (" (FICTIONAL)" if plan.fictional else ""),
                                          "metadata": {"session_plan_sha256": sha, "session_plan_name": plan.name[:200], "session_plan_fictional": plan.fictional,
                                                       "sources_selected": sources}}}
        r = call(client, "POST", f"{args.engine}/api/v1/recordings", json=body)
        if r.status_code == 409:
            out(f"Not started: {r.json().get('detail')}")
            return 2
        r.raise_for_status()
        rid = r.json()["id"]
        out(f"Started recording {rid}.")
    existing = call(client, "GET", f"{args.engine}/api/v1/recordings/{rid}/roster").json()
    done = {(e.get("athlete_id"), e.get("note")) for e in existing}
    rec = call(client, "GET", f"{args.engine}/api/v1/recordings/{rid}").json()
    t0 = rec["started_at"]
    tag = f"session plan {sha[:12]}"
    made = 0
    if (None, tag) not in done:
        c = plan.constraints
        r = call(client, "POST", f"{args.engine}/api/v1/recordings/{rid}/roster", json={
            "effective_from": t0, "source": "head_coach", "note": tag, "substitutions_remaining": c.substitutions_remaining,
            "no_substitution_before": c.no_substitution_before.isoformat() if c.no_substitution_before else None,
            "keep_on_field": [n.model_dump() for n in c.keep_on_field], "do_not_bring_on": [n.model_dump() for n in c.do_not_bring_on]})
        made += r.status_code == 201
    for a in plan.athletes:
        if (a.id, tag) in done:
            continue
        r = call(client, "POST", f"{args.engine}/api/v1/recordings/{rid}/roster", json={
            "athlete_id": a.id, "effective_from": t0, "status": a.status, "current_role": a.current_role if a.status == "on_field" else None,
            "able_roles": a.able_roles, "inputs": a.inputs.model_dump(mode="json"), "source": "assistant_coach", "note": tag})
        if r.status_code != 201:
            out(f"WARNING: roster entry for {a.id} was not stored (HTTP {r.status_code}: {r.text[:200]}); run start again to retry")
        made += r.status_code == 201
    out(f"Roster: {made} entries added, {len(existing)} already present. Monitor with: python scripts/sideline_session.py status --engine {args.engine}")
    return 0


def status_once(client, engine: str, out) -> int:
    act = call(client, "GET", f"{engine}/api/v1/recordings/active").json().get("recording")
    if not act:
        out("No recording is active.")
        return 1
    ev = call(client, "GET", f"{engine}/api/v1/recordings/{act['id']}/evidence", params={"knowledge": "as_entered"}).json()
    groups: dict[str, list[str]] = {}
    for row in ev["athletes"]:
        if "error" in row:
            groups.setdefault("not readable", []).append(row["athlete"]["id"])
            continue
        w = row["evidence"]["sources"]["wearable"]
        groups.setdefault(f"wearable {w['status']}", []).append(row["athlete"]["id"] + (f" ({w['age_s']:.0f} s)" if w.get("age_s") is not None else ""))
    out(f"Recording {act['id']} \"{act['name']}\" active since {act['started_at']} · at {ev['at']}")
    for k in sorted(groups):
        out(f"  {k}: {', '.join(groups[k])}")
    problems = [(r["athlete"]["id"], m) for r in ev["athletes"] if "evidence" in r for m in r["evidence"]["summary"] if m != "all planned inputs present"]
    for aid, m in problems[:20]:
        out(f"  ! {aid}: {m}")
    return 0


def cmd_status(args, client, out) -> int:
    code = 0
    for i in range(args.count if args.watch else 1):
        if i:
            time.sleep(args.watch)
        code = status_once(client, args.engine, out)
    return code


def cmd_stop(args, client, out) -> int:
    rid = args.recording
    if rid is None:
        act = call(client, "GET", f"{args.engine}/api/v1/recordings/active").json().get("recording")
        if not act:
            out("No recording is active; nothing to stop.")
            return 0
        rid = act["id"]
    rec = call(client, "GET", f"{args.engine}/api/v1/recordings/{rid}")
    if rec.status_code == 404:
        out(f"Recording {rid} does not exist.")
        return 2
    if rec.json()["status"] == "stopped":
        out(f"Recording {rid} is already stopped (at {rec.json()['stopped_at']}).")
        return 0
    r = call(client, "POST", f"{args.engine}/api/v1/recordings/{rid}/stop")
    r.raise_for_status()
    out(f"Stopped recording {rid} at {r.json()['stopped_at']}. Inspect with: python scripts/sideline_session.py inspect {rid} --engine {args.engine}")
    return 0


def cmd_inspect(args, client, out) -> int:
    e = args.engine
    r = call(client, "GET", f"{e}/api/v1/recordings/{args.recording}")
    if r.status_code == 404:
        out(f"Recording {args.recording} does not exist.")
        return 2
    rec = r.json()
    snap = (rec.get("configuration_at_start") or {}).get("snapshot") or {}
    meta = ((snap.get("operator_supplied") or {}).get("metadata")) or {}
    out(f"Recording {rec['id']} \"{rec['name']}\" · {rec['status']} · {rec['started_at']} to {rec.get('stopped_at') or 'now'}")
    out(f"  plan: {meta.get('session_plan_name', 'not started from a session plan')}" + (f" (sha256 {meta['session_plan_sha256'][:12]}, sources {meta.get('sources_selected')}"
                                                                                       f"{', FICTIONAL' if meta.get('session_plan_fictional') else ''})" if meta.get("session_plan_sha256") else ""))
    for a in rec["recorded"]:
        p, t = a["physiological"], a["tracking"]
        out(f"  {a['athlete']['id']:<6} physiological {p['sample_count']:>6} (receipts {p['receipt_count']}) · tracking {t['sample_count']:>6} (receipts {t['receipt_count']})")
    roster = call(client, "GET", f"{e}/api/v1/recordings/{rec['id']}/roster").json()
    perf = call(client, "GET", f"{e}/api/v1/recordings/{rec['id']}/performance").json()
    reviews = call(client, "GET", f"{e}/api/v1/recordings/{rec['id']}/reviews").json()
    responses = call(client, "GET", f"{e}/api/v1/recordings/{rec['id']}/responses").json()
    out(f"  roster entries {len(roster)} · performance entries {len(perf) if isinstance(perf, list) else '?'} · saved reviews {len(reviews)} · responses {len(responses)}")
    return 0


def main(argv=None, out=print, client: httpx.Client | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("preflight", "start"):
        p = sub.add_parser(name)
        p.add_argument("plan", type=Path)
        p.add_argument("--engine", default=DEFAULT_ENGINE)
        if name == "preflight":
            p.add_argument("--json", action="store_true")
    p = sub.add_parser("status"); p.add_argument("--engine", default=DEFAULT_ENGINE); p.add_argument("--watch", type=float, default=0); p.add_argument("--count", type=int, default=10)
    p = sub.add_parser("stop"); p.add_argument("--engine", default=DEFAULT_ENGINE); p.add_argument("--recording")
    p = sub.add_parser("inspect"); p.add_argument("recording"); p.add_argument("--engine", default=DEFAULT_ENGINE)
    args = ap.parse_args(argv)
    args.engine = args.engine.rstrip("/")
    own = client is None
    client = client or httpx.Client(timeout=15.0)
    try:
        return {"preflight": cmd_preflight, "start": cmd_start, "status": cmd_status, "stop": cmd_stop, "inspect": cmd_inspect}[args.cmd](args, client, out)
    except EngineDown as exc:
        out(f"The engine at {args.engine} is not reachable ({exc}). Nothing was changed. Start it, or pass --engine.")
        return 3
    except (ValueError, OSError) as exc:
        out(f"Error: {exc}")
        return 2
    finally:
        if own:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
