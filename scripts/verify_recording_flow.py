"""End-to-end check of explicit local recordings on an isolated backend with deliberately small rolling caps.

WHAT IT DOES (fictional athletes and values only; the development backend and database are never touched)
  1. Starts an isolated backend (fresh temporary database, free port) with rolling caps of --cap samples per stream
     (default 3) and registers three fictional athletes.
  2. Starts a recording for the first two athletes, sends --samples samples per stream (default 8, more than the
     caps) for all three athletes, stops the recording, then sends --after more samples per stream (default 4).
  3. Restarts that backend on the same database file.
  4. Verifies, against expectations computed in this script from what it sent: the rolling history holds only its
     capped newest rows (the later ones), while the stopped recording holds exactly the samples accepted for its two
     athletes while it was active and nothing after the stop or for the third athlete; the recording is stopped and
     stays visible after the restart; the session window, summary and effort review match independent expectations
     (distance by the trapezoid rule, heart-rate periods by the documented boundaries) and differ from the rolling
     results as the caps explain; and the recording's export holds every recorded sample and, replayed into a second
     isolated backend with sufficient caps, comes back identical.
  5. Writes a JSON report (reports/ is ignored by git) and prints a summary. Exit 0 when every check passed.

WHAT IT IS NOT
  Not a performance measurement and not a hardware test: a few dozen requests on one machine.

USAGE
    python scripts/verify_recording_flow.py
    python scripts/verify_recording_flow.py --cap 3 --samples 8 --after 4 --report reports/recording-flow.json
"""

import argparse
import json
import signal
import sys
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
for p in (str(ROOT), str(SCRIPTS)):
    if p not in sys.path:
        sys.path.insert(0, p)

import replay_samples  # noqa: E402
from compare_retention import expected_periods, independent_movement  # noqa: E402
from validate_team_scale import IsolatedBackend, RequestRecord  # noqa: E402

ATHLETES = ["fictional-athlete-01", "fictional-athlete-02", "fictional-athlete-03"]
RECORDED = ATHLETES[:2]
STREAMS = ("physiological", "tracking")
STEP_S = 1.0


def iso(ts: datetime) -> str:
    return ts.isoformat().replace("+00:00", "Z")


def payloads(athlete_index: int, k: int, ts: datetime) -> dict[str, dict]:
    """Deterministic fictional samples: heart rate and speed rise with k, offset per athlete; confidence 1.0."""
    return {
        "physiological": {"athlete_id": ATHLETES[athlete_index], "timestamp": iso(ts), "source": "simulated", "device_id": f"fictional-wearable-{athlete_index + 1:02d}", "heart_rate_bpm": 90.0 + 5 * k + athlete_index},
        "tracking": {"athlete_id": ATHLETES[athlete_index], "timestamp": iso(ts), "source": "simulated", "device_id": f"fictional-tracker-{athlete_index + 1:02d}",
                     "x_position": float(k), "y_position": float(athlete_index), "speed_mps": 2.0 + 0.5 * k + 0.1 * athlete_index, "acceleration_mps2": 0.5, "tracking_confidence": 1.0},
    }


def send(client: httpx.Client, base: str, athlete_index: int, k: int, ts: datetime, phase: str) -> list[RequestRecord]:
    out = []
    for stream, body in payloads(athlete_index, k, ts).items():
        r = client.post(f"{base}/api/v1/samples/{stream}", json=body)
        out.append(RequestRecord(ATHLETES[athlete_index], stream, k, 0.0, 0.0, 0.0, "accepted" if r.status_code == 200 else "http_error", r.status_code, detail=phase, payload=body))
    return out


def ts_of(r: RequestRecord) -> datetime:
    return datetime.fromisoformat(r.payload["timestamp"].replace("Z", "+00:00"))


def run(cap: int, samples: int, after: int, out: Callable[[str], None], backend_factory: Callable = IsolatedBackend) -> dict:
    checks: dict = {}
    notes: list[str] = []
    caps = {"physiological": cap, "tracking": cap}
    backend = backend_factory(retention_caps=caps)
    replay_backend = None
    records: list[RequestRecord] = []
    try:
        out(f"Starting an isolated backend on port {backend.port} with rolling caps {cap}/{cap} (database {backend.db_path})…")
        backend.start()
        base = backend.base_url
        with httpx.Client(timeout=10.0) as c:
            for i, a in enumerate(ATHLETES):
                assert c.post(f"{base}/api/v1/athletes", json={"id": a, "name": f"Fictional Athlete {i + 1:02d}", "jersey_number": i + 1, "sport": "fictional", "team_id": "fictional-team"}).status_code == 201
            # samples are timestamped at or just before backend time so the 5 s future rule is never touched
            t0 = datetime.now(UTC) - timedelta(seconds=samples + after + 2)
            started = c.post(f"{base}/api/v1/recordings", json={"name": "Fictional practice", "athlete_ids": RECORDED}).json()
            checks["recording_started_active"] = started.get("status") == "active" and started.get("athlete_ids") == RECORDED
            rec_id = started["id"]
            conflict = c.post(f"{base}/api/v1/recordings", json={"name": "second", "athlete_ids": [ATHLETES[2]]})
            checks["second_start_is_409"] = conflict.status_code == 409
            for k in range(samples):
                for i in range(len(ATHLETES)):
                    records += send(c, base, i, k, t0 + timedelta(seconds=k * STEP_S), "during")
            stopped = c.post(f"{base}/api/v1/recordings/{rec_id}/stop").json()
            checks["stop_returns_stopped"] = stopped.get("status") == "stopped" and stopped.get("stopped_at") is not None
            for k in range(samples, samples + after):
                for i in range(len(ATHLETES)):
                    records += send(c, base, i, k, t0 + timedelta(seconds=k * STEP_S), "after")
            checks["every_sample_accepted"] = all(r.outcome == "accepted" for r in records)
            out(f"Sent {len(records)} samples ({samples} per stream per athlete while recording, {after} after the stop); restarting the backend on the same database…")
        backend.stop(); backend.start()
        with httpx.Client(timeout=10.0) as c:
            detail = c.get(f"{base}/api/v1/recordings/{rec_id}").json()
            checks["recording_visible_and_stopped_after_restart"] = detail.get("status") == "stopped" and detail.get("stopped_at") == stopped.get("stopped_at") and [a["id"] for a in detail["athletes"]] == RECORDED
            checks["no_active_recording_after_restart"] = c.get(f"{base}/api/v1/recordings/active").json() == {"recording": None}
            exp_rec = {a: {s: sorted([r for r in records if r.athlete == a and r.stream == s and r.detail == "during"], key=ts_of) for s in STREAMS} for a in RECORDED}
            exp_roll = {a: {s: sorted([r for r in records if r.athlete == a and r.stream == s], key=ts_of)[-cap:] for s in STREAMS} for a in ATHLETES}
            ok_roll = ok_rec = True
            for a in ATHLETES:
                rd = c.get(f"{base}/api/v1/athletes/{a}/retained-data").json()
                for s in STREAMS:
                    hist = c.get(f"{base}/api/v1/athletes/{a}/{s}/history?limit=300").json()
                    ok_roll = ok_roll and rd[s]["sample_count"] == cap and [h["timestamp"] for h in hist] == [r.payload["timestamp"] for r in exp_roll[a][s]] and rd[s]["max_samples"] == cap
            checks["rolling_history_is_the_capped_newest_including_post_stop_samples"] = ok_roll
            by_id = {e["athlete"]["id"]: e for e in detail["recorded"]}
            for a in RECORDED:
                for s in STREAMS:
                    e = exp_rec[a][s]
                    got = by_id[a][s]
                    ok_rec = ok_rec and got["sample_count"] == len(e) == samples and got["earliest_timestamp"] == e[0].payload["timestamp"] and got["latest_timestamp"] == e[-1].payload["timestamp"]
            checks["recording_holds_exactly_the_samples_accepted_while_active"] = ok_rec
            checks["third_athlete_not_in_recording"] = ATHLETES[2] not in by_id and c.get(f"{base}/api/v1/recordings/{rec_id}/athletes/{ATHLETES[2]}/recorded-data").status_code == 404
            # session window and summary over the whole recording, against independent expectations; rolling results differ as the caps explain
            a = RECORDED[0]
            end = ts_of(exp_rec[a]["tracking"][-1]) + timedelta(seconds=after + 1)   # a window end after the post-stop samples: the recording still excludes them
            win = c.get(f"{base}/api/v1/recordings/{rec_id}/athletes/{a}/window", params={"seconds": samples + after + 5, "end": iso(end)}).json()
            checks["session_window_excludes_post_stop_and_has_no_cap"] = [x["timestamp"] for x in win["tracking_samples"]] == [r.payload["timestamp"] for r in exp_rec[a]["tracking"]] and win["availability"] is None and win["session_availability"]["tracking"]["recorded_count"] == samples and win["recording"]["id"] == rec_id
            roll_win = c.get(f"{base}/api/v1/athletes/{a}/window", params={"seconds": samples + after + 5, "end": iso(end)}).json()
            checks["rolling_window_shows_only_capped_rows"] = len(roll_win["tracking_samples"]) == cap and roll_win["availability"]["tracking"]["at_cap"] is True
            summ = c.get(f"{base}/api/v1/recordings/{rec_id}/athletes/{a}/summary", params={"seconds": samples + after + 5, "end": iso(end)}).json()
            exp_move = independent_movement(exp_rec[a]["tracking"])
            checks["session_summary_matches_independent_distance"] = abs(summ["tracking"]["estimated_distance_m"] - exp_move["estimated_distance_m"]) < 1e-6 and summ["tracking"]["sample_count"] == samples
            # effort over the first half of the recording: before has nothing, during half the samples, after the rest (the recording holds nothing later)
            e_start, e_end = ts_of(exp_rec[a]["physiological"][0]), ts_of(exp_rec[a]["physiological"][samples // 2 - 1])
            review = c.get(f"{base}/api/v1/recordings/{rec_id}/athletes/{a}/effort-review", params={"effort_start": iso(e_start), "effort_end": iso(e_end)}).json()
            exp_hr = expected_periods(exp_rec[a]["physiological"], e_start, e_end, datetime.fromisoformat(review["captured_at"].replace("Z", "+00:00")))
            exp_eff = independent_movement([r for r in exp_rec[a]["tracking"] if e_start <= ts_of(r) <= e_end])
            checks["session_effort_matches_independent_expectations"] = (
                all(review["heart_rate"][n]["sample_count"] == exp_hr[n]["sample_count"] and (review["heart_rate"][n]["heart_rate_mean_bpm"] is None) == (exp_hr[n]["mean"] is None)
                    and (exp_hr[n]["mean"] is None or abs(review["heart_rate"][n]["heart_rate_mean_bpm"] - exp_hr[n]["mean"]) < 1e-6) for n in ("before", "during", "after"))
                and abs(review["movement"]["estimated_distance_m"] - exp_eff["estimated_distance_m"]) < 1e-6 and review["movement"]["sample_count"] == exp_eff["sample_count"]
                and review["session_availability"]["heart_rate"]["after"]["selected_count"] == exp_hr["after"]["sample_count"] and review["availability"] is None)
            roll_review = c.get(f"{base}/api/v1/athletes/{a}/effort-review", params={"effort_start": iso(e_start), "effort_end": iso(e_end)}).json()
            checks["rolling_effort_has_no_samples_and_a_null_distance"] = roll_review["movement"]["sample_count"] == 0 and roll_review["movement"]["estimated_distance_m"] is None and roll_review["availability"]["movement"]["at_cap"] is True
            notes.append(f"session effort for {a}: {review['movement']['sample_count']} tracking samples, distance {review['movement']['estimated_distance_m']:.3f} m; rolling effort over the same period: {roll_review['movement']['sample_count']} samples, distance {roll_review['movement']['estimated_distance_m']}")
            # export every recorded sample, replay into a second isolated backend with sufficient caps, compare
            exp_resp = c.get(f"{base}/api/v1/recordings/{rec_id}/athletes/{a}/recorded-samples.jsonl")
            lines = exp_resp.text.splitlines()
            checks["export_holds_every_recorded_sample"] = exp_resp.status_code == 200 and len(lines) == 2 * samples and exp_resp.headers.get("X-Athletrix-Recording-Id") == rec_id and exp_resp.headers.get("X-Athletrix-Physiological-Count") == str(samples)
            checks["export_is_replay_format_only"] = all(set(json.loads(l)) == {"type", "sample"} for l in lines)
            replay_backend = backend_factory(retention_caps={"physiological": 300, "tracking": 300})
            replay_backend.start()
            with tempfile.TemporaryDirectory(prefix="athletrix-recording-flow-") as tmp:
                path = Path(tmp) / "recording.jsonl"
                path.write_text(exp_resp.text, encoding="utf-8")
                code = replay_samples.main([str(path), "--url", replay_backend.base_url, "--speed", "1000"], out=lambda s: None)
            with httpx.Client(timeout=10.0) as c2:
                again = c2.get(f"{replay_backend.base_url}/api/v1/athletes/{a}/retained-samples.jsonl")
            checks["replay_into_a_separate_database_preserves_every_record"] = code == 0 and again.status_code == 200 and again.text.splitlines() == lines
            notes.append(f"export: {len(lines)} records for {a}; replayed into a second isolated backend (caps 300/300) and exported back identical: {checks['replay_into_a_separate_database_preserves_every_record']}")
    finally:
        backend.cleanup()
        if replay_backend is not None:
            replay_backend.cleanup()
    return {"tool": "verify_recording_flow", "generated_at": datetime.now(UTC).isoformat(), "fictional_data": True, "config": {"rolling_caps": caps, "samples_while_recording": samples, "samples_after_stop": after, "athletes": ATHLETES, "recorded": RECORDED},
            "requests": {"sent": len(records), "accepted": sum(1 for r in records if r.outcome == "accepted")}, "checks": checks, "notes": notes, "passed": all(v is True for v in checks.values()),
            "limitations": ["A functional check with a few dozen requests on one machine; not a performance, hardware or capacity test.", "All athletes and values are fictional."]}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--cap", type=int, default=3, help="rolling cap per stream for the isolated backend (default 3)")
    p.add_argument("--samples", type=int, default=8, help="samples per stream per athlete while recording (default 8; must exceed --cap)")
    p.add_argument("--after", type=int, default=4, help="samples per stream per athlete after the stop (default 4)")
    p.add_argument("--report", type=Path, default=None, help="JSON report path (default reports/recording-flow-<UTC time>.json, ignored by git)")
    return p


def main(argv: list[str] | None = None, *, out: Callable[[str], None] = print, backend_factory: Callable = IsolatedBackend) -> int:
    args = build_parser().parse_args(argv)
    if args.cap < 1 or args.samples <= args.cap or args.samples < 4 or args.after < 1:
        out("cap must be >= 1, samples must exceed the cap and be >= 4, after must be >= 1")
        return 2
    def interrupt(*_):
        raise KeyboardInterrupt
    previous = {s: signal.signal(s, interrupt) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        report = run(args.cap, args.samples, args.after, out, backend_factory)
    except KeyboardInterrupt:
        out("interrupted: the isolated backends started here were stopped and their temporary folders removed")
        return 130
    finally:
        for s, h in previous.items():
            signal.signal(s, h)
    path = args.report or (ROOT / "reports" / f"recording-flow-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    out("RECORDING FLOW CHECK (fictional athletes, isolated backends)")
    for k, v in report["checks"].items():
        out(f"  {'PASS' if v is True else 'FAIL'}  {k}")
    for n in report["notes"]:
        out("  " + n)
    out(f"Requests: {report['requests']['sent']} sent, {report['requests']['accepted']} accepted · JSON report: {path}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
