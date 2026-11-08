"""End-to-end check of the operational prototype against REAL engine processes on isolated temporary databases.

session setup -> paced FICTIONAL inputs -> live team review (as entered) -> engine killed and restarted mid-session ->
the honest state after the outage and the p06 dropout -> a delayed sample -> late annotation and correction -> an
identity correction -> duplicate client action -> operator response and lineup change -> stop -> saved review -> export
-> import into a second engine -> reproduce, an altered and a removed sample detected -> profile change -> removal of an
athlete.

Every engine runs on a free loopback port with its own temporary database; the FICTIONAL live profiles are passed in
the engine's own environment. Nothing touches development data, and only processes started here are stopped.
Camera assignment loss is checked by tests/test_identity_encounters.py and scripts/verify_camera_pipeline.py.

USAGE
    python scripts/verify_operational_workflow.py [--speed 6] [--duration 240] [--report FILE]
Exit code 0 when every check passes.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
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
from validate_team_scale import IsolatedBackend  # noqa: E402


def z(t: datetime) -> str:
    return t.isoformat().replace("+00:00", "Z")


def main(argv=None, out=print) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--speed", type=float, default=6.0)
    ap.add_argument("--duration", type=float, default=240.0, help="scenario seconds")
    ap.add_argument("--report", type=Path)
    args = ap.parse_args(argv)
    if args.report and args.report.exists():
        out(f"{args.report} exists; not overwritten")
        return 2
    checks, measures = [], {}

    def check(name, ok, detail=""):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        out(f"  [{'ok' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))

    env = {k: str(v) for k, v in live_session.LIVE_PROFILES.items()}
    work = Path(tempfile.mkdtemp(prefix="athletrix-ops-"))
    a = IsolatedBackend(extra_env=env)
    b = None
    client = httpx.Client(timeout=15.0)
    try:
        rest_id, bases = live_session.prepare_database(a.db_path, datetime.now(UTC))
        a.start()
        out(f"engine A {a.base_url} (temporary database, FICTIONAL live profiles in its environment)")
        sess = live_session.Session(a.base_url, rest_id, bases, duration_s=args.duration, speed=args.speed, out=lambda s: None, client=client, status_every_s=5)
        rid = sess.setup()
        check("session set up: athletes, accepted fictional baselines, roster, context, recording", rid and not sess.stats.failed, str(sess.stats.failed or "no failures"))
        outage = {}

        def kill_and_restart():
            time.sleep(args.duration / args.speed * 0.35)
            outage["start"] = datetime.now(UTC)
            a.proc.kill(); a.proc.wait(timeout=5)
            time.sleep(2.0)
            a.start()
            outage["end"] = datetime.now(UTC)
        t = threading.Thread(target=kill_and_restart, daemon=True)
        t.start()
        stats = sess.run()
        t.join(timeout=60)
        lost = sum(stats.failed.values())
        check("the engine was killed mid-session and restarted on the same database; sends during the outage failed and were counted, not retried",
              outage.get("end") and lost > 0, f"{lost} failed sends ({stats.failed}) during {(outage['end'] - outage['start']).total_seconds():.1f} s")
        rec = client.get(f"{a.base_url}/api/v1/recordings/{rid}").json()
        check("the active recording survived the restart", rec["status"] == "active")
        tr = client.get(f"{a.base_url}/api/v1/recordings/{rid}/team-review", params={"knowledge": "as_entered"}).json()
        measures["team_review_ms"] = [r["compute_ms"] for r in stats.reviews] + [tr["compute_ms"]]
        by = {x["athlete"]["id"]: x for x in tr["athletes"]}
        check("one team review covers every athlete with one captured time and one profile set", tr["counts"]["reviewed"] == len(live_session.PLAYERS) and tr["knowledge"] == "as_entered",
              f"{tr['counts']} in {tr['compute_ms']} ms")
        p06 = by["p06"]
        check("p06's stopped strap shows an old reading and no current index (a stale value is not shown as current)",
              p06["latest_reading"]["age_s"] > 15 and not p06["physiological_index"]["available"], f"reading {p06['latest_reading']['age_s']} s old; {p06['physiological_index']['reasons'][:1]}")
        # targeted reproduction of the reported p06 defect: the strap stops, performance evidence stays available
        stop_txt = next((e.split(" ", 1)[0] for e in stats.events if "p06's strap stops" in e), None)
        stale_after = json.loads(Path(env["PHYSIOLOGICAL_INDEX_PROFILE_PATH"]).read_text())["staleness_seconds"]
        after = [r for r in stats.reviews if stop_txt and r["at"] and datetime.fromisoformat(r["at"].replace("Z", "+00:00")) >
                 datetime.fromisoformat(stop_txt.replace("Z", "+00:00")) + timedelta(seconds=stale_after)]
        kept = [r["at"] for r in after if r["outcomes"].get("p06") == "keep"]
        p06_hist = [h["outcome"] for h in p06["review"]["history"]]
        check("p06 (strap stopped, performance still available) is 'insufficient evidence' in every live review once its last reading is stale; never 'keep'",
              after and not kept and p06["outcome"] == "insufficient_evidence" and p06["performance"]["available"]
              and p06["review"]["reasons"][0].startswith("required input unavailable: physiological index"),
              f"{len(after)} periodic reviews after the strap stopped + {stale_after:g} s, keep in {len(kept)}; now {p06['outcome']} "
              f"(performance available: {p06['performance']['available']}); history {'keep' in p06_hist and 'had keep before the dropout' or 'no keep in lookback'}")
        healthy = [a for a in ("p01", "p03", "p04", "p07") if by[a]["outcome"] in ("keep", "review_later")]
        check("players beside p06 with complete evidence are unaffected by its missing data", len(healthy) == 4, f"{[(a, by[a]['outcome']) for a in ('p01', 'p03', 'p04', 'p07')]}")
        # honest state right after the outage: ages grew through it; nothing was filled in for the missing seconds
        t_end = outage["end"] - timedelta(seconds=0.3)   # just before the restarted engine accepted its first sample
        ev_out = client.get(f"{a.base_url}/api/v1/recordings/{rid}/evidence", params={"knowledge": "as_entered", "at": z(t_end)}).json()
        ages = [r["evidence"]["sources"]["wearable"].get("age_s") for r in ev_out["athletes"] if r["athlete"]["id"] in ("p01", "p02", "p03")]
        gap = (t_end - outage["start"]).total_seconds()
        check("at the end of the outage the wearable readings are as old as the outage (no value invented for the missing seconds)",
              all(x is not None and x >= gap - 1.1 for x in ages), f"ages {ages} s, {gap:.1f} s after the engine was killed (one reading per second)")
        live = [x["latest_reading"] for x in tr["athletes"] if x["reviewed"] and x["latest_reading"]]
        check("live readings keep observation time and receipt time apart (receipt recorded by this engine)",
              live and all(r["received_at"] and r["availability"] == "received_by_review_time" for r in live), f"{len(live)} readings")

        # late annotation, then a correction
        now = datetime.now(UTC)
        # b02 (bench) has no other evidence, so the overlap rule cannot set this aside as a potential duplicate
        period = {"athlete_id": "b02", "role": "central midfielder", "area": "execution", "kind": "pass", "period_start": z(now - timedelta(seconds=20)), "period_end": z(now - timedelta(seconds=10)),
                  "assessment": "assessed", "attempts": 10, "completions": 2, "annotator": "verifier (fictional)"}
        first = client.post(f"{a.base_url}/api/v1/recordings/{rid}/performance", json=period).json()
        time.sleep(1.2)
        t_mid = datetime.now(UTC)
        time.sleep(1.2)
        fix = client.post(f"{a.base_url}/api/v1/recordings/{rid}/performance", json={**period, "completions": 9, "supersedes": first["id"], "correction_reason": "recounted (fictional)"}).json()
        pr = lambda at, k: client.get(f"{a.base_url}/api/v1/recordings/{rid}/athletes/b02/performance-review", params={"at": z(at), "knowledge": k}).json()  # noqa: E731
        before_entry = pr(now - timedelta(seconds=5), "as_entered")
        retro = pr(now - timedelta(seconds=5), "retrospective")
        mid = pr(t_mid, "as_entered")
        ids = lambda r: set(r["counted_observation_ids"])  # noqa: E731
        check("a late annotation is absent from the live (as-entered) view of the time before it was entered", first["id"] not in ids(before_entry) and fix["id"] not in ids(before_entry))
        check("the retrospective view uses the correction once and says it was entered later", fix["id"] in ids(retro) and first["id"] not in ids(retro) and retro["review_time"]["entered_after_review_time"] >= 1,
              retro["review_time"]["label"][:90])
        check("between entry and correction the as-entered view counts the original, not the later correction", first["id"] in ids(mid) and fix["id"] not in ids(mid))

        # a delayed sample: observed 8 s before it is received; the live view of the time before its receipt does not use it
        t_obs = datetime.now(UTC) - timedelta(seconds=8)
        late = {"athlete_id": "b02", "timestamp": z(t_obs), "source": "simulated", "device_id": "fictional-handheld-b02", "heart_rate_bpm": 77.0, "signal_quality": 0.9}
        before_receipt = datetime.now(UTC)
        time.sleep(0.3)
        client.post(f"{a.base_url}/api/v1/samples/physiological", json=late)
        ev_b = lambda t: next(r for r in client.get(f"{a.base_url}/api/v1/recordings/{rid}/evidence", params={"knowledge": "as_entered", "at": z(t)}).json()["athletes"]  # noqa: E731
                              if r["athlete"]["id"] == "b02")["evidence"]["sources"]["wearable"]
        then, now_ = ev_b(before_receipt), ev_b(datetime.now(UTC))
        check("a delayed sample is not used for a time before it arrived, and its delay is visible afterwards",
              then.get("last_observed_at") != z(t_obs) and now_["last_observed_at"] == z(t_obs).replace("+00:00", "Z") and now_["receipt_delay_s"] >= 7.5,
              f"before receipt: newest {then.get('last_observed_at')}; after: {now_.get('last_observed_at')} with delay {now_.get('receipt_delay_s')} s ({now_.get('status')})")
        # an identity correction on manual evidence: entered uncertain, corrected to confirmed later
        t_id = datetime.now(UTC)
        uo = {"athlete_id": "b04", "role": "forward", "area": "execution", "kind": "turnover", "period_start": z(t_id - timedelta(seconds=40)), "period_end": z(t_id - timedelta(seconds=30)),
              "assessment": "assessed", "possessions": 4, "turnovers": 1, "identity": "uncertain", "annotator": "verifier (fictional)"}
        u1 = client.post(f"{a.base_url}/api/v1/recordings/{rid}/performance", json=uo).json()
        time.sleep(1.2)
        t_mid_id = datetime.now(UTC)
        time.sleep(1.2)
        u2 = client.post(f"{a.base_url}/api/v1/recordings/{rid}/performance", json={**uo, "identity": "confirmed", "supersedes": u1["id"], "correction_reason": "identity confirmed on review (fictional)"}).json()
        rv = lambda t: client.get(f"{a.base_url}/api/v1/recordings/{rid}/athletes/b04/performance-review", params={"at": z(t), "knowledge": "as_entered"}).json()  # noqa: E731
        r_mid, r_now = rv(t_mid_id), rv(datetime.now(UTC))
        check("an identity correction: before it, the uncertain entry is listed but not counted; after it, the confirmed version counts once",
              u1["id"] not in r_mid["counted_observation_ids"] and u2["id"] in r_now["counted_observation_ids"] and u1["id"] not in r_now["counted_observation_ids"],
              f"counted before {len(r_mid['counted_observation_ids'])}, after {len(r_now['counted_observation_ids'])}")

        # duplicate client action, operator response, lineup state
        saved = client.post(f"{a.base_url}/api/v1/recordings/{rid}/athletes/p02/reviews", json={"kind": "substitution", "at": tr["at"], "knowledge": "as_entered", "label": "verifier"}).json()
        body = {"athlete_id": "p02", "kind": "keep", "client_token": "verify-keep-0001", "saved_review_id": saved["id"], "reason": "verifier: simulated decision", "simulated": True}
        r1, r2 = client.post(f"{a.base_url}/api/v1/recordings/{rid}/responses", json=body), client.post(f"{a.base_url}/api/v1/recordings/{rid}/responses", json=body)
        stored = [x for x in client.get(f"{a.base_url}/api/v1/recordings/{rid}/responses").json() if x["client_token"] == "verify-keep-0001"]
        check("a repeated client action is stored once", r1.status_code == 201 and r2.status_code == 200 and len(stored) == 1)
        ros = client.get(f"{a.base_url}/api/v1/recordings/{rid}/roster/effective", params={"at": z(datetime.now(UTC))}).json()
        check("the session's simulated lineup change is in the roster state", ros["athletes"]["p05"]["status"] == "unavailable" and ros["athletes"]["b03"]["status"] == "on_field"
              and ros["team"]["substitutions_remaining"] == 2, f"p05 {ros['athletes']['p05']['status']}, b03 {ros['athletes']['b03']['status']}, substitutions {ros['team']['substitutions_remaining']}")
        client.post(f"{a.base_url}/api/v1/recordings/{rid}/stop")
        late_save = client.post(f"{a.base_url}/api/v1/recordings/{rid}/athletes/p01/reviews", json={"kind": "substitution", "at": z(t_mid), "knowledge": "retrospective"}).json()
        package = client.get(f"{a.base_url}/api/v1/recordings/{rid}/package.zip").content
        a.stop()

        # second engine: import, reproduce, profile change
        b = IsolatedBackend(extra_env=env)
        from app.core.recording_package import read_package  # noqa: PLC0415
        from app.db import Database  # noqa: PLC0415
        from app.store.recording_import import import_package  # noqa: PLC0415
        pkg = read_package(package)
        db = Database(b.db_path)
        rid2 = import_package(db, pkg, datetime.now(UTC)).recording_id
        db.close()
        b.start()
        reviews = client.get(f"{b.base_url}/api/v1/recordings/{rid2}/reviews").json()
        reps = [client.get(f"{b.base_url}/api/v1/recordings/{rid2}/reviews/{v['id']}/reproduce").json() for v in reviews]
        check("every saved review reproduces on a second engine after export and import", reps and all(r["status"] == "reproduced" for r in reps), f"{len(reps)} reviews, package format {pkg.manifest['format_version']}")
        # an altered and a removed recorded sample are detected when reproducing on the second engine
        sub = next(v for v in reviews if v["kind"] == "substitution")
        con = sqlite3.connect(b.db_path)
        try:
            row = con.execute("SELECT ts_utc, payload FROM recording_physiological_samples WHERE recording_id = ? AND athlete_id = ? AND ts_utc <= ? ORDER BY ts_utc DESC LIMIT 1",
                              (rid2, sub["athlete_id"], sub["at"].replace("Z", "+00:00"))).fetchone()
            rep_status = lambda: client.get(f"{b.base_url}/api/v1/recordings/{rid2}/reviews/{sub['id']}/reproduce").json()["status"]  # noqa: E731
            d = json.loads(row[1]); d["heart_rate_bpm"] = (d.get("heart_rate_bpm") or 100) + 3
            con.execute("UPDATE recording_physiological_samples SET payload = ? WHERE recording_id = ? AND athlete_id = ? AND ts_utc = ?", (json.dumps(d), rid2, sub["athlete_id"], row[0])); con.commit()
            altered = rep_status()
            con.execute("UPDATE recording_physiological_samples SET payload = ? WHERE recording_id = ? AND athlete_id = ? AND ts_utc = ?", (row[1], rid2, sub["athlete_id"], row[0])); con.commit()
            restored = rep_status()
            full = con.execute("SELECT * FROM recording_physiological_samples WHERE recording_id = ? AND athlete_id = ? AND ts_utc = ?", (rid2, sub["athlete_id"], row[0])).fetchone()
            con.execute("DELETE FROM recording_physiological_samples WHERE recording_id = ? AND athlete_id = ? AND ts_utc = ?", (rid2, sub["athlete_id"], row[0])); con.commit()
            removed = rep_status()
            cols = [c[1] for c in con.execute("PRAGMA table_info(recording_physiological_samples)")]
            con.execute(f"INSERT INTO recording_physiological_samples ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", full); con.commit()
        finally:
            con.close()
        check("an altered and a removed recorded sample are both detected by reproduction; the restored data reproduces again",
              altered == "evidence_changed" and removed == "evidence_changed" and restored == "reproduced", f"altered {altered}, restored {restored}, removed {removed}")
        resp2 = client.get(f"{b.base_url}/api/v1/recordings/{rid2}/responses").json()
        rev_ids = {v["id"] for v in reviews}
        check("imported responses point at the imported saved reviews", resp2 and all(r["saved_review_id"] in rev_ids for r in resp2 if r["saved_review_id"]), f"{len(resp2)} responses")
        b.stop()
        changed = json.loads(Path(env["SUBSTITUTION_PROFILE_PATH"]).read_text())
        changed.update(version="fictional-live-substitution-1-changed", cooldown_seconds=10)
        alt = work / "changed.json"
        alt.write_text(json.dumps(changed))
        b.extra_env = {**env, "SUBSTITUTION_PROFILE_PATH": str(alt)}
        b.start()
        rep = client.get(f"{b.base_url}/api/v1/recordings/{rid2}/reviews/{reviews[0]['id']}/reproduce").json()
        check("after a profile change a saved review still reproduces from its pinned profile", rep["status"] == "reproduced" and rep["configuration_differs_from_current"]["substitution"])
        b.stop()

        # removal on a copy of the second database
        copy = work / "removal.db"
        shutil.copy(b.db_path, copy)
        import importlib.util  # noqa: PLC0415
        spec = importlib.util.spec_from_file_location("local_data_ops", ROOT / "scripts" / "local_data.py")
        ld = importlib.util.module_from_spec(spec); spec.loader.exec_module(ld)
        lines = []
        code = ld.main(["remove-athlete", "p01", "--database", str(copy), "--execute", "--confirm", "p01"], out=lines.append)
        con = sqlite3.connect(copy)
        dangling = con.execute("SELECT COUNT(*) FROM recording_responses r WHERE json_extract(r.payload, '$.saved_review_id') IS NOT NULL AND NOT EXISTS "
                               "(SELECT 1 FROM recording_reviews v WHERE v.id = json_extract(r.payload, '$.saved_review_id'))").fetchone()[0]
        left = con.execute("SELECT COUNT(*) FROM recording_performance WHERE athlete_id = 'p01'").fetchone()[0]
        subs = con.execute("SELECT COUNT(*) FROM recording_reviews WHERE kind = 'substitution'").fetchone()[0]
        con.close()
        check("removing an athlete leaves no response pointing at a removed review, and none of the athlete's evidence (corrections included)",
              code == 0 and dangling == 0 and left == 0 and subs == 0, f"dangling {dangling}, p01 observations {left}, substitution reviews left {subs}")
        check("a retrospective review saved after the stop keeps its retrospective label", late_save["inputs"]["knowledge"] == "retrospective" and late_save["result"]["knowledge"]["mode"] == "retrospective")
    finally:
        client.close()
        a.cleanup()
        if b is not None:
            b.cleanup()
        shutil.rmtree(work, ignore_errors=True)
    failed = [c for c in checks if not c["ok"]]
    if measures.get("team_review_ms"):
        ms = sorted(measures["team_review_ms"])
        out(f"team review compute time on this machine ({len(ms)} reviews of {len(live_session.PLAYERS)} athletes): median {ms[len(ms) // 2]} ms, max {ms[-1]} ms (one laptop; not a capacity claim)")
    out(f"{len(checks) - len(failed)}/{len(checks)} checks passed (FICTIONAL session).")
    if args.report:
        args.report.write_text(json.dumps({"checks": checks, "measures": measures, "fictional": True}, indent=2))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
