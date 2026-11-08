"""An isolated FICTIONAL rehearsal of the science-fair fatigue pilot, end to end.

baseline rest recording -> study session with a frozen profile -> one vest, three participants in turn (each with their
OWN baseline) -> an engine restart after the first participant -> a round marked invalid -> drill rounds with 1 Hz heart rate and 10 Hz camera speed -> a pre-sprint score saved at every timed sprint
-> independent sprint times entered afterwards -> a wearer change -> a dropout before one sprint and recovery -> camera-only
observations of two other players (never combined scores) -> stop -> export (package format 8) -> import into a second
isolated database -> reproduce every saved prediction -> local removal of one participant on a copy (the exported package
keeps them) -> the paired report (scripts/evaluate.py pilot).

MODES (never confused in the output)
  --mode accelerated  in-process engine with a simulated clock: minutes of scenario in seconds of wall time. A software
                      check of the workflow; NOT a timed run and never a "90-minute sideline run".
  --mode wall         a real engine process on a free loopback port, samples sent at their real times (wall-clock
                      ingest). --duration-min 90 is the one-laptop reliability check: it also polls the pilot view once a
                      second through the API, samples the engine's memory and reports lateness, failures and gaps.
Neither mode involves a real camera, video processing, a wearable or a person. Sprint times come from a SEPARATE seeded
outcome model driven by the scenario's hidden fatigue level, never from the scores under test.

USAGE
    python scripts/pilot_rehearsal.py --mode accelerated --out DIR
    python scripts/pilot_rehearsal.py --mode wall --rounds 2 --out DIR          # about 9 minutes
    python scripts/pilot_rehearsal.py --mode wall --duration-min 90 --out DIR   # the 90-minute reliability check
    (--hold keeps a wall-mode engine running after the export for a browser check; --profile chooses the fatigue profile:
     default scripts/examples/pilot/fictional_rehearsal_profile.json, FICTIONAL short windows)
Exit 0 when the workflow completed with no failed request and every prediction reproduced.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import live_session  # noqa: E402

PROFILE = ROOT / "scripts" / "examples" / "pilot" / "fictional_rehearsal_profile.json"
VEST = "fictional-vest-1"
# FICTIONAL participants: pseudonymous codes only, made-up ages for an estimated HR max (with its origin stated)
PARTICIPANTS = [("P1", "px1", 16, 3.05), ("P2", "px2", 17, 3.20), ("P3", "px3", 16, 3.12)]
CAMERA_ONLY = ["cx1", "cx2"]


def z(t: datetime) -> str:
    return t.isoformat().replace("+00:00", "Z")


class Api:
    def __init__(self, base: str, client) -> None:
        self.base, self.c = base.rstrip("/"), client   # base without /api/v1
        self.failed: dict[str, int] = {}
        self.sent: dict[str, int] = {}
        self.lat: dict[str, list] = {}

    def call(self, what: str, method: str, path: str, **kw):
        t0 = time.perf_counter()
        try:
            r = self.c.request(method, self.base + "/api/v1" + path, **kw)
            ok = r.status_code in (200, 201)
        except httpx.HTTPError:
            r, ok = None, False
        self.lat.setdefault(what, []).append(time.perf_counter() - t0)
        self.sent[what] = self.sent.get(what, 0) + 1
        if not ok:
            self.failed[what] = self.failed.get(what, 0) + 1
            return None
        return r.json() if r.content else {}


class Scenario:
    """Hidden fictional fatigue (separate seeds for readings and for sprint times)."""

    def __init__(self, seed: str = "athletrix-pilot-rehearsal") -> None:
        self.latent = random.Random(seed + "-latent")
        self.readings = random.Random(seed + "-readings")
        self.outcome = random.Random(seed + "-outcome")
        self.level = {}

    def fatigue(self, code: str, rnd: int) -> float:
        k = (code, rnd)
        if k not in self.level:
            self.level[k] = max(0.0, min(1.0, 0.25 * (rnd - 1) + self.latent.gauss(0, 0.08) + {"P1": 0.0, "P2": 0.1, "P3": 0.05}.get(code, 0)))
        return self.level[k]

    def hr(self, rest: float, hrmax: float, f: float, phase: float, sprint: bool) -> float:
        if sprint:
            return min(hrmax - 3, rest + (hrmax - rest) * 0.95) + self.readings.gauss(0, 2)
        effort = 0.12 if (phase % 20) < 6 else 0.0
        return rest + (hrmax - rest) * (0.55 + 0.25 * f + effort) + self.readings.gauss(0, 2.5)

    def speed(self, f: float, phase: float, sprint: bool) -> float:
        if sprint:
            return 7.4 - 1.2 * f + self.readings.gauss(0, 0.15)
        if (phase % 20) < 2.0:   # a short burst in play: a sprint opportunity
            return 6.8 - 1.5 * f + self.readings.gauss(0, 0.2)
        return max(0.0, 3.0 * (1 - 0.3 * f) + self.readings.gauss(0, 0.3))

    def sprint_time(self, base: float, f: float) -> float:
        return round(base * (1 + 0.06 * f + self.outcome.gauss(0, 0.008)), 3)


def run(args, api: Api, now, sleep, out, restart=None) -> dict:
    sc = Scenario()
    prof_text = Path(args.profile).read_text()
    log = {"rounds": [], "events": []}
    for code, aid, _, _ in PARTICIPANTS:
        api.call("setup", "POST", "/athletes", json={"id": aid, "name": f"Fictional participant {code}", "jersey_number": int(aid[2:]) + 10, "sport": "soccer", "team_id": "fictional-pilot"})
    for aid in CAMERA_ONLY:
        api.call("setup", "POST", "/athletes", json={"id": aid, "name": f"Fictional camera-only player {aid}", "jersey_number": int(aid[2:]) + 30, "sport": "soccer", "team_id": "fictional-pilot"})
    rest = api.call("setup", "GET", f"/recordings/{args.rest_id}")
    base_ids = {}
    for code, aid, _, _ in PARTICIPANTS:
        b = api.call("setup", "POST", f"/athletes/{aid}/baselines", json={"source_recording_id": args.rest_id, "ranges": [{"start": rest["started_at"], "end": rest["stopped_at"]}],
                                                                       "condition": "seated_rest", "signals": ["heart_rate_bpm"], "min_samples": 60, "label": "FICTIONAL rest",
                                                                       "created_by": "fictional operator"})
        api.call("setup", "POST", f"/athletes/{aid}/baselines/{b['id']}/decision", json={"decision": "accepted", "decided_by": "fictional operator", "note": "FICTIONAL"})
        base_ids[aid] = (b["id"], b["stats"]["heart_rate_bpm"]["mean"])
    rec = api.call("setup", "POST", "/recordings", json={"name": "FICTIONAL fatigue pilot rehearsal", "athlete_ids": [p[1] for p in PARTICIPANTS] + CAMERA_ONLY})
    rid = rec["id"]
    log["recording_id"] = rid
    tok = iter(range(10 ** 6))

    def entry(**kw):
        return api.call("study", "POST", f"/recordings/{rid}/study", json={"client_token": f"reh-{next(tok):05d}-{kw['kind'][:6]}", **kw})
    entry(kind="session", session_label="rehearsal-day1", protocol="rehearsal", fictional=True, planned_rounds=args.rounds,
          clock_references=[{"stream": "camera", "basis": "operator_estimate", "uncertainty_s": 0.04, "note": "FICTIONAL: no camera; stated for the rehearsal"}])
    dropout = ("P2", 2)
    poll_stop = threading.Event()
    polls = []

    def poller():
        while not poll_stop.wait(1.0):
            t0 = time.perf_counter()
            r = api.call("pilot_view", "GET", f"/recordings/{rid}/study/pilot", params={"trend_span_s": 120, "trend_step_s": 15})
            if r is not None:
                polls.append({"latency_s": time.perf_counter() - t0, "wearer": (r.get("wearer") or {}).get("participant_code"),
                              "combined": ((r.get("estimate") or {}).get("candidates") or {}).get("combined", {}).get("value")})
    if args.mode == "wall":
        threading.Thread(target=poller, daemon=True).start()
    lateness = []
    rounds_total = 0
    deadline = now() + timedelta(minutes=args.duration_min) if args.duration_min else None
    cycle = 0
    while True:
        for code, aid, age, base_time in PARTICIPANTS:
            if deadline and now() >= deadline:
                break
            hrmax = 208 - 0.7 * age
            rest_hr = base_ids[aid][1]
            entry(kind="wearer", athlete_id=aid, participant_code=code, device_id=VEST, baseline_id=base_ids[aid][0], hr_max_bpm=round(hrmax, 1),
                  hr_max_origin="FICTIONAL estimate: 208 - 0.7 x made-up age")
            log["events"].append(f"{z(now())} wearer {code}")
            for k in range(args.rounds):
                rnd = cycle * args.rounds + k + 1
                if deadline and now() >= deadline:
                    break
                f = sc.fatigue(code, rnd)
                entry(kind="round_start", athlete_id=aid, round=rnd)
                start = now()
                step, nxt, t_hr = 0.1, start, start
                drop = (code, rnd) == dropout
                cam_gap = (code, rnd) == ("P3", 2) and cycle == 0   # the camera loses this player for the last 15 s before the save
                while (now() - start).total_seconds() < args.round_s:
                    target = nxt
                    w = (target - now()).total_seconds()
                    if w > 0:
                        sleep(w)
                    lateness.append(max(0.0, (now() - target).total_seconds()))
                    t = now()
                    ph = (t - start).total_seconds()
                    if (t - t_hr).total_seconds() >= 1.0 - 1e-6:
                        t_hr = t
                        if not (drop and ph > args.round_s - args.dropout_s):
                            api.call("physiological", "POST", "/samples/physiological", json={"athlete_id": aid, "timestamp": z(t), "source": "simulated", "device_id": VEST,
                                                                                               "heart_rate_bpm": round(sc.hr(rest_hr, hrmax, f, ph, False), 1)})
                    for who, ff in [(aid, f)] + [(c, 0.3) for c in CAMERA_ONLY]:
                        if who == aid and cam_gap and ph > args.round_s - 15:
                            continue
                        api.call("tracking", "POST", "/samples/tracking", json={"athlete_id": who, "timestamp": z(t), "x_position": 0.0, "y_position": 0.0,
                                                                              "speed_mps": round(sc.speed(ff, ph + (7 if who != aid else 0), False), 2), "acceleration_mps2": 0.0,
                                                                              "tracking_confidence": 0.9, "source": "camera", "segment_id": f"seg-{who}", "value_basis": "estimated"})
                    nxt = target + timedelta(seconds=step)
                t0 = time.perf_counter()
                missed = (code, rnd) == ("P2", 1) and cycle == 0   # the operator missed "save score": the order cannot be verified
                saved = None
                if not missed:
                    saved = entry(kind="score_save", athlete_id=aid, round=rnd)
                pred_ms = 1000 * (time.perf_counter() - t0)
                sleep(1.0)   # the operator reads the confirmation, then starts the sprint
                ref = None
                if (code, rnd) == ("P1", 2) and cycle == 0:   # a timing gate with a recorded clock check (FICTIONAL)
                    ref = {"source": "timing_gate", "device_time": z(now() + timedelta(seconds=0.5) - timedelta(seconds=0.25)), "clock_offset_s": 0.25,
                           "clock_uncertainty_s": 0.05, "mapping_reference": "FICTIONAL gate clock check"}
                mark = entry(kind="sprint_start", athlete_id=aid, round=rnd, **({"confirms_prediction_id": saved["id"]} if saved else {}), **({"start_reference": ref} if ref else {}))
                for i in range(int(args.sprint_s * 10)):
                    sleep(0.1)
                    t = now()
                    if i % 10 == 0:
                        api.call("physiological", "POST", "/samples/physiological", json={"athlete_id": aid, "timestamp": z(t), "source": "simulated", "device_id": VEST,
                                                                                           "heart_rate_bpm": round(sc.hr(rest_hr, hrmax, f, 0, True), 1)})
                    api.call("tracking", "POST", "/samples/tracking", json={"athlete_id": aid, "timestamp": z(t), "x_position": 0.0, "y_position": 0.0, "speed_mps": round(sc.speed(f, 0, True), 2),
                                                                          "acceleration_mps2": 0.0, "tracking_confidence": 0.9, "source": "camera", "segment_id": f"seg-{aid}", "value_basis": "estimated"})
                entry(kind="sprint_end", athlete_id=aid, round=rnd)
                if (code, rnd) == ("P1", 3):
                    entry(kind="round_end", athlete_id=aid, round=rnd, invalid=True, notes="FICTIONAL: false start, the sprint is not valid")
                else:
                    entry(kind="round_end", athlete_id=aid, round=rnd)
                log["rounds"].append({"participant": code, "athlete_id": aid, "round": rnd, "hidden_fatigue": round(f, 3), "sprint_start_id": (mark or {}).get("id"),
                                      "saved_prediction_id": (saved or {}).get("id"), "all_three_available": ((saved or {}).get("prediction") or {}).get("all_three_available"),
                                      "save_missed": missed, "gate_start_reference": bool(ref), "camera_gap_before_save": cam_gap,
                                      "prediction_save_ms": round(pred_ms, 1), "dropout_before_sprint": drop, "base_time": base_time})
                rounds_total += 1
            # independent results through the blind timing sheet (a separate outcome model; the timer never sees a score)
            todo = [x for x in log["rounds"] if x["participant"] == code and "time_s" not in x]
            import csv as _csv  # noqa: PLC0415
            import sprint_timing  # noqa: PLC0415
            sheet = Path(tempfile.mkdtemp(prefix="athletrix-timing-")) / "times.csv"
            tl: list[str] = []
            sprint_timing.main(["--engine", api.base, "--recording", rid, "sheet", "--out", str(sheet)], out=tl.append, client=api.c)
            srows = list(_csv.DictReader(open(sheet)))
            for r in todo:
                row = next(x for x in srows if (x["participant_code"], x["round"]) == (code, str(r["round"])))
                if (code, r["round"]) == ("P3", 1):
                    row.update(missing_reason="equipment_failure", notes="FICTIONAL: gate battery", timer="FICTIONAL timer")
                    r["time_s"] = None
                elif (code, r["round"]) == ("P1", 3):
                    row.update(invalid_reason="FICTIONAL: false start", timer="FICTIONAL timer")
                    r["time_s"] = None
                else:
                    t_s = sc.sprint_time(base_time, r["hidden_fatigue"])
                    row.update(method="timing_gates", time_s=str(t_s), precision_s="0.01", timer="FICTIONAL timer")
                    r["time_s"] = t_s
            with open(sheet, "w", newline="") as fh:
                wr = _csv.DictWriter(fh, fieldnames=sprint_timing.COLUMNS); wr.writeheader(); [wr.writerow(x) for x in srows if any(x.get(k) for k in ("time_s", "missing_reason", "invalid_reason"))]
            code_v = sprint_timing.main(["--engine", api.base, "--recording", rid, "apply", str(sheet), "--yes"], out=tl.append, client=api.c)
            log.setdefault("timing_sheet", []).append({"participant": code, "exit": code_v, "summary": tl[-1] if tl else None})
            for r in todo:
                if r["round"] % 2 == 0:
                    entry(kind="self_rating", athlete_id=aid, round=r["round"], missing_reason="not_collected")
                else:
                    entry(kind="self_rating", athlete_id=aid, round=r["round"], value=round(min(10, 3 + 5 * r["hidden_fatigue"] + sc.outcome.gauss(0, 0.5)), 1), scale="FICTIONAL 0-10")
            sleep(args.rest_s)
            if restart is not None and code == "P1" and cycle == 0:
                t_r = time.perf_counter()
                restart()
                log["events"].append(f"{z(now())} engine restarted")
                log["restart"] = {"after": "P1's rounds", "seconds": round(time.perf_counter() - t_r, 2),
                                  "recording_still_active": (api.call("setup", "GET", "/recordings/active") or {}).get("recording", {}).get("id") == rid}
        cycle += 1
        if not deadline or now() >= deadline:
            break
    entry(kind="wearer", athlete_id=PARTICIPANTS[-1][1], end=True)
    poll_stop.set()
    pv = api.call("pilot_view", "GET", f"/recordings/{rid}/study/score", params={"athlete_id": CAMERA_ONLY[0]})
    log["camera_only_player"] = {"athlete": CAMERA_ONLY[0], "combined": pv["candidates"]["combined"] if pv else None, "vest_only": pv["candidates"]["vest_only"] if pv else None}
    api.call("setup", "POST", f"/recordings/{rid}/stop")
    log["lateness_s"] = {"p50": round(statistics.median(lateness), 4), "p95": round(sorted(lateness)[int(0.95 * len(lateness))], 4), "max": round(max(lateness), 4), "steps": len(lateness)} if lateness else None
    log["pilot_view_polls"] = {"count": len(polls), "latency_s_p50": round(statistics.median([p["latency_s"] for p in polls]), 4) if polls else None,
                               "latency_s_p95": round(sorted(p["latency_s"] for p in polls)[int(0.95 * len(polls))], 4) if polls else None,
                               "latency_s_max": round(max(p["latency_s"] for p in polls), 4) if polls else None}
    log["rounds_total"] = rounds_total
    del prof_text
    return log


def finish(args, api: Api, log: dict, out) -> dict:
    """Export, import into a second isolated database, reproduce every prediction, and run the paired report."""
    rid = log["recording_id"]
    pkg_bytes = api.c.get(api.base + f"/api/v1/recordings/{rid}/package.zip").content
    pkg_path = args.out / "pilot-package.zip"
    pkg_path.write_bytes(pkg_bytes)
    from app.core.recording_package import read_package  # noqa: PLC0415
    from app.db import Database  # noqa: PLC0415
    from app.store.recording_import import import_package  # noqa: PLC0415
    pkg = read_package(pkg_bytes)
    db_path = Path(tempfile.mkdtemp(prefix="athletrix-pilot-import-")) / "import.db"
    db = Database(db_path)
    new = import_package(db, pkg, datetime.now(UTC)).recording_id
    db.close()
    rep = subprocess.run([sys.executable, "-c", (
        "import json,sys;from fastapi.testclient import TestClient;from app.db import Database,set_database;from app.main import create_app;"
        f"set_database(Database({str(db_path)!r}));c=TestClient(create_app());"
        f"es=c.get('/api/v1/recordings/{new}/study').json()['entries'];"
        f"print(json.dumps([c.get('/api/v1/recordings/{new}/study/predictions/'+e['id']+'/reproduce').json()['status'] for e in es if e['kind']=='prediction']))")],
        cwd=ROOT, capture_output=True, text=True, env={**os.environ, "FATIGUE_PROFILE_PATH": ""})
    statuses = json.loads(rep.stdout.strip().splitlines()[-1]) if rep.returncode == 0 else [f"error: {rep.stderr[-300:]}"]
    import shutil  # noqa: PLC0415
    import sqlite3  # noqa: PLC0415
    copy = db_path.parent / "removal.db"
    shutil.copy(db_path, copy)
    spec_ld = __import__("importlib.util").util.spec_from_file_location("local_data_pilot", ROOT / "scripts" / "local_data.py")
    ld = __import__("importlib.util").util.module_from_spec(spec_ld); spec_ld.loader.exec_module(ld)
    rl = []
    rcode = ld.main(["remove-athlete", PARTICIPANTS[0][1], "--database", str(copy), "--execute", "--confirm", PARTICIPANTS[0][1]], out=rl.append)
    con = sqlite3.connect(copy)
    left = con.execute("SELECT COUNT(*) FROM recording_study WHERE athlete_id = ?", (PARTICIPANTS[0][1],)).fetchone()[0]
    others = con.execute("SELECT COUNT(*) FROM recording_study WHERE athlete_id != ? OR athlete_id IS NULL", (PARTICIPANTS[0][1],)).fetchone()[0]
    con.close()
    removal = {"athlete": PARTICIPANTS[0][0], "exit": rcode, "study_entries_left_for_them": left, "other_study_entries_kept": others,
               "exported_package_still_holds_them": sum(1 for e in pkg.study if e.athlete_id == PARTICIPANTS[0][1]),
               "note": "local removal deletes this engine's copy only; packages already exported (and their copies) are not touched"}
    lines = []
    import importlib.util  # noqa: PLC0415
    spec = importlib.util.spec_from_file_location("evaluate_pilot_reh", ROOT / "scripts" / "evaluate.py")
    ev = importlib.util.module_from_spec(spec); spec.loader.exec_module(ev)
    code = ev.main(["pilot", "--packages", str(pkg_path), "--out", str(args.out / "report")], out=lines.append)
    res = json.loads((args.out / "report" / "results.json").read_text()) if code == 0 else {}
    return {"package": {"file": pkg_path.name, "format_version": pkg.manifest["format_version"], "study_entries": len(pkg.study), "predictions": sum(1 for e in pkg.study if e.kind == "prediction")},
            "removal_on_a_copy": removal,
            "reproduction_on_second_database": {"statuses": statuses, "all_reproduced": bool(statuses) and all(s == "reproduced" for s in statuses)},
            "report": {"exit": code, "matched_rows": res.get("matched_rows"), "coverage": {c: v["usable"] for c, v in res.get("coverage", {}).items()},
                       "pooled_r": {c: res["pooled"][c]["r"] for c in ("vest_only", "camera_only", "combined")} if res else None, "targets": res.get("targets")}}


def main(argv=None, out=print) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["accelerated", "wall"], required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--profile", type=Path, default=PROFILE)
    ap.add_argument("--rounds", type=int, default=3, help="rounds per participant per turn")
    ap.add_argument("--round-s", type=float, default=70.0)
    ap.add_argument("--sprint-s", type=float, default=4.0)
    ap.add_argument("--rest-s", type=float, default=5.0)
    ap.add_argument("--dropout-s", type=float, default=20.0)
    ap.add_argument("--duration-min", type=float, default=0.0, help="wall mode: keep rotating participants for this long (the reliability check)")
    ap.add_argument("--hold", action="store_true", help="wall mode: keep the engine running after the export until Ctrl-C")
    ap.add_argument("--port", type=int)
    ap.add_argument("--cors-origin", default="http://127.0.0.1:3918")
    args = ap.parse_args(argv)
    if args.out.exists():
        out(f"{args.out} exists; not overwritten")
        return 2
    if args.mode == "accelerated" and args.duration_min:
        out("--duration-min is a wall-clock reliability check; the accelerated mode is never a timed run")
        return 2
    args.out.mkdir(parents=True)
    players = [(aid, f"Fictional participant {code}", int(aid[2:]) + 10, False, None, [], True) for code, aid, _, _ in PARTICIPANTS]
    started = time.monotonic()
    summary = {"mode": args.mode, "fictional": True, "profile": Path(args.profile).name, "started_at": z(datetime.now(UTC)),
               "not_exercised": ["a real camera or video processing", "a real wearable or the Zephyr path", "any person"]}
    if args.mode == "accelerated":
        summary["label"] = "ACCELERATED in-process replay with a simulated clock: a software check, not a timed sideline run"
        from fastapi.testclient import TestClient  # noqa: PLC0415

        from app.core import clock  # noqa: PLC0415
        from app.core.config import get_settings  # noqa: PLC0415
        from app.db import Database, set_database  # noqa: PLC0415
        tmp = Path(tempfile.mkdtemp(prefix="athletrix-pilot-acc-"))
        state = {"now": datetime.now(UTC).replace(microsecond=0) - timedelta(hours=6)}
        rest_id, _ = live_session.prepare_database(tmp / "pilot.db", state["now"], players=players)
        os.environ["FATIGUE_PROFILE_PATH"] = str(args.profile)
        get_settings.cache_clear()
        saved = (clock.utc_now, clock.receipt_now)
        clock.utc_now = clock.receipt_now = lambda: state["now"]
        db = Database(tmp / "pilot.db")
        set_database(db)
        try:
            from app.main import create_app  # noqa: PLC0415
            client = TestClient(create_app())
            api = Api("http://testserver", client)
            args.rest_id = rest_id
            state["now"] += timedelta(minutes=1)

            def sleep(s):
                state["now"] += timedelta(seconds=s)
            box = {"db": db}

            def restart():
                box["db"].close()
                box["db"] = Database(tmp / "pilot.db")
                set_database(box["db"])
                api.c = TestClient(create_app())
            log = run(args, api, lambda: state["now"], sleep, out, restart)
            db = box["db"]
            fin = finish(args, api, log, out)
        finally:
            clock.utc_now, clock.receipt_now = saved
            set_database(None)
            db.close()
            os.environ.pop("FATIGUE_PROFILE_PATH", None)
            get_settings.cache_clear()
    else:
        summary["label"] = "WALL-CLOCK ingest against a real engine process on this laptop (fictional inputs; no camera, wearable or video processing)"
        from validate_team_scale import IsolatedBackend  # noqa: PLC0415
        eng = IsolatedBackend(port=args.port, extra_env={"FATIGUE_PROFILE_PATH": str(args.profile)}, cors_origins=[args.cors_origin])
        rest_id, _ = live_session.prepare_database(eng.db_path, datetime.now(UTC), players=players)
        eng.start()
        out(f"engine {eng.base_url} (temporary database); pilot view: GET {eng.base_url}/api/v1/recordings/<id>/study/pilot")
        (args.out / "engine.json").write_text(json.dumps({"engine": eng.base_url, "pid": eng.proc.pid, "database": str(eng.db_path)}))
        rss, rss_stop = [], threading.Event()

        def sample_rss():
            while not rss_stop.wait(30):
                try:
                    kb = int(subprocess.run(["ps", "-o", "rss=", "-p", str(eng.proc.pid)], capture_output=True, text=True).stdout.strip() or 0)
                    rss.append(kb / 1024)
                except (OSError, ValueError):
                    pass
        threading.Thread(target=sample_rss, daemon=True).start()
        client = httpx.Client(timeout=15.0)
        api = Api(eng.base_url, client)
        args.rest_id = rest_id
        try:
            def restart():
                eng.proc.terminate(); eng.proc.wait(timeout=10); eng.proc = None
                eng.start()
            log = run(args, api, lambda: datetime.now(UTC), time.sleep, out, restart)
            fin = finish(args, api, log, out)
            summary["engine_memory_mb"] = {"samples": len(rss), "first": round(rss[0], 1) if rss else None, "max": round(max(rss), 1) if rss else None, "last": round(rss[-1], 1) if rss else None}
            summary["engine_alive_at_end"] = eng.proc.poll() is None
            if args.hold:
                import signal  # noqa: PLC0415
                release = args.out / "release"
                (args.out / "summary-before-hold.json").write_text(json.dumps({**summary, **fin, "failed_requests": api.failed, "requests": api.sent,
                                                                                "log": {k: v for k, v in log.items() if k != "events"}}, indent=2, default=str))
                out(f"holding the engine for a browser check; stop with Ctrl-C, SIGTERM or by creating {release}")
                signal.signal(signal.SIGTERM, lambda *_: release.touch())
                try:
                    while not release.exists():
                        time.sleep(1)
                except KeyboardInterrupt:
                    pass
        finally:
            rss_stop.set()
            client.close()
            eng.cleanup()
    summary.update(log={k: v for k, v in log.items() if k != "events"}, **fin, failed_requests=api.failed, requests=api.sent,
                   request_latency_ms={k: {"p50": round(1000 * statistics.median(v), 2), "p95": round(1000 * sorted(v)[int(0.95 * len(v))], 2), "max": round(1000 * max(v), 2)} for k, v in api.lat.items()},
                   wall_seconds=round(time.monotonic() - started, 1))
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    out(json.dumps({k: summary[k] for k in ("mode", "label", "wall_seconds", "failed_requests", "reproduction_on_second_database", "report")}, indent=1, default=str))
    rem = fin["removal_on_a_copy"]
    ok = (not {k: v for k, v in api.failed.items() if k != "pilot_view"} and summary["reproduction_on_second_database"]["all_reproduced"] and fin["report"]["exit"] == 0
          and rem["exit"] == 0 and rem["study_entries_left_for_them"] == 0 and rem["other_study_entries_kept"] > 0)
    out("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
