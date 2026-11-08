"""Team-scale exercise of the sideline rehearsal workflow on ONE isolated engine (FICTIONAL athletes and readings).

20 fictional athletes (11 on the field, 9 on the bench), accepted fictional baselines, the FICTIONAL live profiles in the
engine's own environment, a session plan started with scripts/sideline_session.py (inputs: wearable 1 Hz, camera 10 Hz),
then for --duration seconds: physiology at 1 Hz and camera-derived tracking at 10 Hz for every athlete, performance
entries for the starters every 20 s (entered 2 s after each period), and two starters' wearables paused from 1/4 to 3/4 of
the run. Every 2 s it reads the team review (knowledge=as_entered), which now carries the evidence view. Reported:
inputs sent / accepted / failed, write latency, team-review latency and compute time per player, and the stale-state
behaviour of the paused players (status and outcome on every read). One laptop: observations, not a capacity claim.

USAGE
    python scripts/rehearsal_load.py [--duration 60] [--athletes 20] [--report FILE]
Exit code 0 when no write failed and the paused players were never shown as "keep" while their wearable was stale.
"""

from __future__ import annotations

import argparse
import json
import queue
import random
import statistics
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
import sideline_session  # noqa: E402
from validate_team_scale import IsolatedBackend  # noqa: E402


def pct(v: list[float], q: float) -> float | None:
    v = sorted(v)
    return None if not v else round(v[min(len(v) - 1, int(q * len(v)))], 2)


def players(n: int) -> list:
    out = []
    roles = ["goalkeeper", "right back", "centre back", "centre back", "left back", "central midfielder", "central midfielder", "central midfielder", "winger", "forward", "winger"]
    for i in range(n):
        starter = i < 11
        role = roles[i] if starter else None
        able = [roles[i]] if starter else [roles[(i * 3) % 11], roles[(i * 5 + 1) % 11]]
        out.append((f"x{i + 1:02d}", f"Fictional Player {i + 1:02d}", i + 1, starter, role, able, True))
    return out


def main(argv=None, out=print) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--duration", type=float, default=60.0)
    ap.add_argument("--athletes", type=int, default=20)
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--report", type=Path)
    args = ap.parse_args(argv)
    P = players(args.athletes)
    paused = {"x06", "x07"}
    env = {k: str(v) for k, v in live_session.LIVE_PROFILES.items()}
    eng = IsolatedBackend(extra_env=env)
    client = httpx.Client(timeout=10.0, limits=httpx.Limits(max_connections=args.workers + 4))
    rng = random.Random("athletrix-rehearsal-load")
    try:
        rest_id, bases = live_session.prepare_database(eng.db_path, datetime.now(UTC), players=P)
        eng.start()
        e = eng.base_url
        out(f"engine {e} (temporary database, FICTIONAL live profiles)")
        for pid, *_ in P:
            client.post(f"{e}/api/v1/athletes", json={"id": pid, "name": f"Fictional Player {pid[1:]}", "jersey_number": int(pid[1:]), "sport": "soccer", "team_id": "fictional-load"})
        rest = client.get(f"{e}/api/v1/recordings/{rest_id}").json()
        for pid, *_ in P:
            b = client.post(f"{e}/api/v1/athletes/{pid}/baselines", json={"source_recording_id": rest_id, "ranges": [{"start": rest["started_at"], "end": rest["stopped_at"]}],
                                                                        "condition": "seated_rest", "signals": ["heart_rate_bpm", "breathing_rate_bpm", "hrv_rmssd_ms"], "min_signal_quality": 0.6,
                                                                        "min_samples": 120, "label": "FICTIONAL", "created_by": "fictional analyst"}).json()
            client.post(f"{e}/api/v1/athletes/{pid}/baselines/{b['id']}/decision", json={"decision": "accepted", "decided_by": "fictional analyst", "note": "FICTIONAL"})
        plan = {"format": "athletrix-session-plan", "schema_version": 1, "name": "FICTIONAL rehearsal load", "fictional": True, "team_id": "fictional-load",
                "athletes": [{"id": pid, "name": f"Fictional Player {pid[1:]}", "jersey_number": n, "status": "on_field" if st else "bench", "current_role": role, "able_roles": able,
                              "inputs": {"physiology": "expected", "physiology_interval_s": 1.0, "camera": "expected", "camera_interval_s": 0.1}, "wearable_device_id": f"fictional-strap-{pid}"}
                             for pid, _, n, st, role, able, _ in P],
                "sources": {"wearable": {"selected": True}, "camera": {"selected": True}}, "constraints": {"substitutions_remaining": 3}, "min_free_storage_mb": 100}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(plan, f)
        lines = []
        code = sideline_session.main(["start", f.name, "--engine", e], out=lines.append, client=client)
        Path(f.name).unlink()
        if code != 0:
            out("session start failed:\n" + "\n".join(lines))
            return 1
        rid = client.get(f"{e}/api/v1/recordings/active").json()["recording"]["id"]
        out(f"session started from a plan: {rid}")
        tasks: queue.Queue = queue.Queue()
        results: list[tuple] = []
        lock = threading.Lock()

        def worker():
            while True:
                item = tasks.get()
                if item is None:
                    return
                kind, url, body, due = item
                t0 = time.monotonic()
                try:
                    r = client.post(url, json=body)
                    ok = r.status_code in (200, 201)
                except httpx.HTTPError:
                    ok = False
                with lock:
                    results.append((kind, ok, time.monotonic() - t0, t0 - due))
        threads = [threading.Thread(target=worker, daemon=True) for _ in range(args.workers)]
        for t in threads:
            t.start()
        reads, read_stop = [], threading.Event()

        def reader():
            while not read_stop.wait(2.0):
                t0 = time.monotonic()
                try:
                    r = client.get(f"{e}/api/v1/recordings/{rid}/team-review", params={"knowledge": "as_entered"}).json()
                except (httpx.HTTPError, ValueError):
                    reads.append({"failed": True})
                    continue
                lat = time.monotonic() - t0
                row = {"latency_ms": round(1000 * lat, 1), "compute_ms": r["compute_ms"], "reviewed": r["counts"]["reviewed"], "at": r["at"],
                       "paused": {x["athlete"]["id"]: {"status": x["evidence"]["sources"]["wearable"]["status"], "outcome": x["outcome"]} for x in r["athletes"] if x["athlete"]["id"] in paused},
                       "outcomes": r["counts"]["outcomes"]}
                reads.append(row)
        rt = threading.Thread(target=reader, daemon=True)
        rt.start()
        start = time.monotonic()
        t_pause = (args.duration / 4, 3 * args.duration / 4)
        period_start = datetime.now(UTC)
        omitted = 0
        steps = int(args.duration * 10)
        pause_wall = {}
        for k in range(steps):
            due = start + k / 10
            wait = due - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            now = datetime.now(UTC)
            ts = now.isoformat().replace("+00:00", "Z")
            el = k / 10
            for pid, *_ in P:
                tasks.put(("tracking", f"{e}/api/v1/samples/tracking", {"athlete_id": pid, "timestamp": ts, "x_position": round(50 + 10 * rng.random(), 2), "y_position": round(30 + 10 * rng.random(), 2),
                                                                         "speed_mps": round(3 * rng.random(), 2), "acceleration_mps2": 0.0, "tracking_confidence": 0.9, "source": "camera",
                                                                         "device_id": "fictional-camera", "segment_id": f"seg-{pid}", "value_basis": "estimated"}, due))
            if k % 10 == 0:
                for pid, *_ in P:
                    if pid in paused and t_pause[0] <= el < t_pause[1]:
                        omitted += 1
                        pause_wall.setdefault("start", now)
                        pause_wall["end"] = now
                        continue
                    lv = 1.0 if int(pid[1:]) <= 11 else 0.15
                    tasks.put(("physiological", f"{e}/api/v1/samples/physiological", live_session.physiological(pid, lv, bases[pid], rng, now), due))
            if k and k % 200 == 0:
                for pid, _, _, st, role, *_ in P:
                    if st:
                        base = {"athlete_id": pid, "role": role, "period_start": period_start.isoformat(), "period_end": now.isoformat(), "assessment": "assessed", "annotator": "fictional analyst (simulated)"}
                        for extra in ({"area": "execution", "kind": "pass", "attempts": 5, "completions": 4}, {"area": "execution", "kind": "turnover", "possessions": 5, "turnovers": 1},
                                      {"area": "decision_making", "kind": "decision", "opportunities": 2, "rated_good": 1, "rated_acceptable": 1, "rated_poor": 0, "criterion": "fictional"},
                                      {"area": "movement_positioning", "kind": "positioning", "opportunities": 2, "rated_good": 2, "rated_acceptable": 0, "rated_poor": 0, "criterion": "fictional"},
                                      {"area": "coach_strategy", "kind": "tactical_observation", "instruction": "fictional", "outcome": "followed"}):
                            tasks.put(("performance", f"{e}/api/v1/recordings/{rid}/performance", {**base, **extra}, due))
                period_start = now
        for _ in threads:
            tasks.put(None)
        for t in threads:
            t.join(timeout=30)
        read_stop.set()
        rt.join(timeout=10)
        stale_after = json.loads(Path(env["PHYSIOLOGICAL_INDEX_PROFILE_PATH"]).read_text())["staleness_seconds"]
        during = [r for r in reads if "at" in r and pause_wall and datetime.fromisoformat(r["at"].replace("Z", "+00:00")) > pause_wall["start"] + timedelta(seconds=stale_after + 1)
                  and datetime.fromisoformat(r["at"].replace("Z", "+00:00")) < pause_wall["end"]]
        after = [r for r in reads if "at" in r and pause_wall and datetime.fromisoformat(r["at"].replace("Z", "+00:00")) > pause_wall["end"] + timedelta(seconds=15)]
        kept_while_stale = sum(1 for r in during for v in r["paused"].values() if v["outcome"] == "keep")
        stale_seen = {v["status"] for r in during for v in r["paused"].values()}
        recovered = {v["status"] for r in after for v in r["paused"].values()}
        by = {}
        for kind, ok, lat, late in results:
            d = by.setdefault(kind, {"sent": 0, "accepted": 0, "failed": 0, "lat": [], "late": []})
            d["sent"] += 1; d["accepted"] += ok; d["failed"] += not ok; d["lat"].append(1000 * lat); d["late"].append(1000 * late)
        ok_reads = [r for r in reads if "at" in r]
        report = {"fictional": True, "athletes": args.athletes, "duration_s": args.duration, "omitted_by_scenario": omitted,
                  "inputs": {k: {"sent": v["sent"], "accepted": v["accepted"], "failed": v["failed"], "write_ms": {"p50": pct(v["lat"], .5), "p95": pct(v["lat"], .95), "max": pct(v["lat"], 1)},
                                 "scheduling_lateness_ms_p95": pct(v["late"], .95)} for k, v in by.items()},
                  "team_review": {"reads": len(reads), "failed": len(reads) - len(ok_reads), "latency_ms": {"p50": pct([r["latency_ms"] for r in ok_reads], .5), "p95": pct([r["latency_ms"] for r in ok_reads], .95),
                                                                                                         "max": pct([r["latency_ms"] for r in ok_reads], 1)},
                                  "compute_ms_per_player": {"p50": pct([r["compute_ms"] / max(1, r["reviewed"]) for r in ok_reads], .5), "max": pct([r["compute_ms"] / max(1, r["reviewed"]) for r in ok_reads], 1)}},
                  "stale_state": {"paused_players": sorted(paused), "reads_while_stale": len(during), "statuses_while_stale": sorted(stale_seen), "keep_while_stale": kept_while_stale,
                                  "statuses_15s_after_resume": sorted(recovered)},
                  "machine_note": "one laptop, the engine and this load generator on the same machine; not a capacity claim"}
        for k, v in report["inputs"].items():
            out(f"  {k:<13} sent {v['sent']:>6} accepted {v['accepted']:>6} failed {v['failed']:>3} · write p50 {v['write_ms']['p50']} ms p95 {v['write_ms']['p95']} ms max {v['write_ms']['max']} ms")
        tr = report["team_review"]
        out(f"  team review: {tr['reads']} reads ({tr['failed']} failed) · latency p50 {tr['latency_ms']['p50']} ms p95 {tr['latency_ms']['p95']} ms max {tr['latency_ms']['max']} ms · "
            f"compute per player p50 {tr['compute_ms_per_player']['p50']} ms")
        ss = report["stale_state"]
        out(f"  paused wearables {ss['paused_players']}: {ss['reads_while_stale']} reads while stale, statuses {ss['statuses_while_stale']}, shown as keep {ss['keep_while_stale']} times; "
            f"15 s after resuming: {ss["statuses_15s_after_resume"]}")
        if args.report:
            args.report.write_text(json.dumps(report, indent=2))
        failed = sum(v["failed"] for v in report["inputs"].values())
        good = (failed == 0 and ss["reads_while_stale"] > 0 and ss["keep_while_stale"] == 0 and set(ss["statuses_while_stale"]) <= {"stale", "disconnected"}
                and ss["statuses_15s_after_resume"] == ["present"])
        out("PASS" if good else "FAIL")
        return 0 if good else 1
    finally:
        client.close()
        eng.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
