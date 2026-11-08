"""End-to-end check of the local session workflow (scripts/sideline_session.py) against REAL engine processes on
isolated temporary databases and free loopback ports. FICTIONAL plan and readings only.

startup failure (a database path that cannot be created) -> a normal engine start with no profiles -> preflight
(raw recording ready, experimental recommendation blocked) -> start -> start again (resumes, repeats nothing) ->
another plan refused while active -> fictional readings for some planned wearables only -> status (the silent ones are
missing, nothing filled in) -> the engine killed and restarted -> status again (still recording) -> stop -> stop again
-> inspect -> the engine stopped cleanly. Only processes started here are stopped.

USAGE
    python scripts/verify_session_workflow.py [--report FILE]
Exit code 0 when every check passes.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import sideline_session as ss  # noqa: E402
from validate_team_scale import IsolatedBackend  # noqa: E402

PLAN = ROOT / "scripts" / "examples" / "session" / "fictional_session_plan.json"


def port_free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def main(argv=None, out=print) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report", type=Path)
    args = ap.parse_args(argv)
    checks = []

    def check(name, ok, detail=""):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        out(f"  [{'ok' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))

    def cli(*a):
        lines = []
        code = ss.main(list(a), out=lines.append)
        return code, "\n".join(lines)

    # 1. startup failure: the database path's parent is a regular file, so the engine cannot open its database
    with tempfile.TemporaryDirectory(prefix="athletrix-session-") as tmp:
        blocker = Path(tmp) / "not-a-directory"
        blocker.write_text("x")
        bad = IsolatedBackend()
        bad.db_path = blocker / "engine.db"
        try:
            bad.start(ready_timeout_s=15)
            started = True
        except RuntimeError as exc:
            started, why = False, str(exc)
        log = bad.log_path.read_text(errors="replace") if bad.log_path.exists() else ""
        code, text = cli("preflight", str(PLAN), "--engine", bad.base_url)
        check("an engine that cannot open its database does not start, and the session tool says nothing was changed",
              not started and (bad.proc is None or bad.proc.poll() is not None) and code == 3 and "Nothing was changed" in text, (why.split(";")[0] if not started else "started") + "; " + next((ln.strip()[:100] for ln in reversed(log.splitlines()) if "Error" in ln), "no error line in the log"))
        bad.cleanup()

    eng = IsolatedBackend()          # no profile variables: the engine's normal startup
    try:
        eng.start()
        e = eng.base_url
        out(f"engine {e} (temporary database, normal startup: no profiles)")
        prof = httpx.get(f"{e}/api/v1/profiles").json()
        check("normal startup enables no profile (none ships)", all(v is None for v in prof["profiles"].values()), json.dumps({k: v for k, v in prof["profiles"].items()}))
        code, text = cli("preflight", str(PLAN), "--engine", e)
        pf = httpx.post(f"{e}/api/v1/preflight", json=ss.load_plan(PLAN).model_dump(mode="json")).json()
        check("preflight: raw recording not blocked, experimental recommendation blocked by the missing profiles",
              code == 0 and pf["readiness"]["raw_recording"]["verdict"] != "blocked" and pf["readiness"]["experimental_recommendation"]["verdict"] == "blocked",
              f"raw {pf['readiness']['raw_recording']['verdict']}, camera {pf['readiness']['camera_review']['verdict']}, recommendation {pf['readiness']['experimental_recommendation']['verdict']}")
        check("preflight reports every planned wearable as not receiving (no reading is invented)", all(not a["wearable"]["receiving"] for a in pf["athletes"]))
        code, text = cli("start", str(PLAN), "--engine", e)
        rid = httpx.get(f"{e}/api/v1/recordings/active").json()["recording"]["id"]
        check("start registers the plan's athletes, starts one recording and enters the roster with input plans", code == 0 and "Roster: 7 entries added" in text, rid)
        code, text = cli("start", str(PLAN), "--engine", e)
        roster = httpx.get(f"{e}/api/v1/recordings/{rid}/roster").json()
        check("starting the same plan again resumes it and repeats nothing", code == 0 and "Resuming" in text and len(roster) == 7, f"{len(roster)} roster entries")
        other = json.loads(PLAN.read_text()); other["name"] = "FICTIONAL other plan"
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(other, f)
        code, text = cli("start", f.name, "--engine", e)
        Path(f.name).unlink()
        check("a different plan is refused while this recording is active", code == 2 and "another recording is active" in text)
        now = datetime.now(UTC)
        sent = 0
        for s in range(10, 0, -1):
            for aid in ("f01", "f02"):
                r = httpx.post(f"{e}/api/v1/samples/physiological", json={"athlete_id": aid, "timestamp": (now - timedelta(seconds=s)).isoformat(), "source": "simulated",
                                                                           "device_id": f"fictional-strap-{aid}", "heart_rate_bpm": 120.0 + s, "signal_quality": 0.9})
                sent += r.status_code == 200
        code, text = cli("status", "--engine", e)
        check("status: the sending wearables are present, the silent planned ones unavailable, the not-used ones not used",
              code == 0 and "wearable present: f01" in text and "f02" in text.split("wearable present:")[1].split("\n")[0]
              and "wearable unavailable: f03, f05" in text and "wearable not_used: f04, f06" in text, text.replace("\n", " | ")[:220])
        eng.proc.kill(); eng.proc.wait(timeout=5); eng.proc = None
        code, text = cli("status", "--engine", e)
        check("while the engine is down the tool reports it unreachable", code == 3)
        eng.start()
        code, text = cli("status", "--engine", e)
        check("after a restart the same recording is still active and reported", code == 0 and rid in text)
        code, text = cli("start", str(PLAN), "--engine", e)
        check("start after the restart resumes; still no duplicate entries", code == 0 and "Resuming" in text and len(httpx.get(f"{e}/api/v1/recordings/{rid}/roster").json()) == 7)
        c1, t1 = cli("stop", "--engine", e)
        c2, t2 = cli("stop", "--engine", e, "--recording", rid)
        check("stop, then stop again reports it is already stopped", c1 == 0 and "Stopped recording" in t1 and c2 == 0 and "already stopped" in t2)
        code, text = cli("inspect", rid, "--engine", e)
        check("inspect shows the plan metadata and the recorded counts", code == 0 and "FICTIONAL rehearsal session" in text and "physiological     10 (receipts 10)" in text,
              text.split("\n")[1][:120] if "\n" in text else text)
        port = eng.port
        eng.stop()
        check("the engine started here stops cleanly and frees its port", eng.proc is None and port_free(port), f"port {port}")
    finally:
        eng.cleanup()
    failed = [c for c in checks if not c["ok"]]
    out(f"{len(checks) - len(failed)}/{len(checks)} checks passed (FICTIONAL plan, isolated engines).")
    if args.report:
        args.report.write_text(json.dumps({"checks": checks, "fictional": True}, indent=2))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
