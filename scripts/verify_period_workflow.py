"""End-to-end check of saved effort periods and their review snapshots, on isolated backends with fictional data.

WHAT IT DOES (the development backend, database and recordings are never touched)
  1. Starts an isolated backend (fresh temporary database, free port), registers two fictional athletes, starts a
     recording for both and feeds a deterministic fictional session built from the simulator's effort-rest profile
     (40 s cycles: accelerate, sustain, slow, rest) at 1 Hz for --seconds, with missing heart-rate measurements,
     low-confidence tracking points and a gap in both streams, then stops the recording. A second recording with a
     few samples is started and stopped so separation can be checked.
  2. Saves several periods for the first athlete (an acceleration, a sustained effort, a rest, the gap, and two with
     the same label), one for the second athlete and one in the second recording, downloads each period's review
     snapshot, then restarts the backend on the same database file.
  3. Verifies: every saved period is listed again with its id and exact bounds; identical labels coexist; periods of
     the other athlete and the other recording are separate; reopening a period through the session effort review
     gives figures equal to expectations computed independently here from the samples that were sent (trapezoid
     distance over eligible intervals, threshold crossings, the documented heart-rate phase boundaries, null heart
     rates as samples but not measurements); an empty phase and a missing distance stay null; each snapshot's
     figures, counts, rules and samples agree with the live review; and, after restarting the backend with another
     high-speed threshold, a new review and a new snapshot reflect it while the saved bounds and the earlier
     downloaded snapshot do not change.
  4. Writes a JSON report (reports/ is ignored by git) and prints a summary. Exit 0 when every check passed.

WHAT IT IS NOT
  A functional check with a few hundred requests on one machine; not a performance, hardware or capacity test.

USAGE
    python scripts/verify_period_workflow.py
    python scripts/verify_period_workflow.py --seconds 120 --report reports/period-workflow.json
"""

import argparse
import json
import os
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

from compare_retention import close, expected_periods, independent_movement  # noqa: E402
from simulate_athlete import effort_rest_at, effort_rest_heart_rate_series  # noqa: E402
from validate_team_scale import IsolatedBackend, RequestRecord  # noqa: E402

ATHLETES = ["fictional-athlete-01", "fictional-athlete-02"]
NULL_HR_EVERY = 9        # every ninth second the heart rate is missing (a null measurement)
LOW_CONF_EVERY = 13      # every thirteenth second the tracking point has low confidence
GAP = (50, 56)           # seconds [50, 56): nothing sent in either stream
CHANGED_THRESHOLD = "4.0"


def iso(ts: datetime) -> str:
    return ts.isoformat().replace("+00:00", "Z")


def ts_of(r: RequestRecord) -> datetime:
    return datetime.fromisoformat(r.payload["timestamp"].replace("Z", "+00:00"))


def session_payloads(athlete_index: int, second: int, hr_series: list[float], ts: datetime) -> dict[str, dict] | None:
    """The fictional sample pair for one athlete at one second of the session, or None inside the gap."""
    if GAP[0] <= second < GAP[1]:
        return None
    hr, speed, x, acc = effort_rest_at(second, hr_series)
    return {
        "physiological": {"athlete_id": ATHLETES[athlete_index], "timestamp": iso(ts), "source": "simulated", "device_id": f"fictional-wearable-{athlete_index + 1:02d}",
                          "heart_rate_bpm": None if second % NULL_HR_EVERY == 0 else round(hr + athlete_index, 1)},
        "tracking": {"athlete_id": ATHLETES[athlete_index], "timestamp": iso(ts), "source": "simulated", "device_id": f"fictional-tracker-{athlete_index + 1:02d}",
                     "x_position": x, "y_position": float(athlete_index), "speed_mps": speed, "acceleration_mps2": acc,
                     "tracking_confidence": 0.2 if second % LOW_CONF_EVERY == 0 else 1.0},
    }


def period_plan(t0: datetime) -> list[dict]:
    """The periods saved for the first athlete: offsets from the session start, in whole seconds."""
    return [
        {"label": "acceleration", "start": 0, "end": 10},
        {"label": "sustained", "start": 10, "end": 20},
        {"label": "rest", "start": 30, "end": 40},
        {"label": "gap", "start": GAP[0], "end": GAP[1] - 1},       # nothing recorded inside: empty phases, null distance
        {"label": "second cycle effort", "start": 40, "end": 60},   # crosses the gap and the second cycle's acceleration
        {"label": "second cycle effort", "start": 80, "end": 100},  # the same label again: labels are not identifiers
    ]


def send(client: httpx.Client, base: str, payloads: dict) -> list[RequestRecord]:
    out = []
    for stream, body in payloads.items():
        r = client.post(f"{base}/api/v1/samples/{stream}", json=body)
        out.append(RequestRecord(body["athlete_id"], stream, 0, 0.0, 0.0, 0.0, "accepted" if r.status_code == 200 else "http_error", r.status_code, payload=body))
    return out


def start_backend(backend: IsolatedBackend, env: dict[str, str] | None = None) -> None:
    """Start (or restart) the isolated backend; `env` goes only into that process's environment."""
    saved = {k: os.environ.get(k) for k in (env or {})}
    try:
        for k, v in (env or {}).items():
            os.environ[k] = v
        backend.start()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def review_matches_expectation(review: dict, phys: list, track: list, start: datetime, end: datetime, threshold: float) -> tuple[bool, dict]:
    """Compare a session effort review with expectations computed here from the sent samples under the newest rules."""
    captured = datetime.fromisoformat(review["captured_at"].replace("Z", "+00:00"))
    exp_hr = expected_periods(phys, start, end, captured)
    in_effort = [r for r in track if start <= ts_of(r) <= end]
    exp_move = independent_movement(in_effort, threshold_mps=threshold)
    m = review["movement"]
    ok = (m["sample_count"] == exp_move["sample_count"] and close(m["estimated_distance_m"], exp_move["estimated_distance_m"])
          and close(m["included_duration_seconds"], exp_move["included_duration_seconds"]) and m["excluded_interval_count"] == exp_move["excluded_interval_count"]
          and close(m["high_speed_duration_seconds"], exp_move["high_speed_duration_seconds"]) and close(m["high_speed_distance_m"], exp_move["high_speed_distance_m"]))
    for name in ("before", "during", "after"):
        p, e = review["heart_rate"][name], exp_hr[name]
        ok = ok and p["sample_count"] == e["sample_count"] and close(p["heart_rate_mean_bpm"], e["mean"])
        ok = ok and (p["first_sample_at"] is None) == (e["first"] is None)
    ok = ok and (exp_move["sample_count"] >= 2 or m["estimated_distance_m"] is None)
    return ok, {"expected_movement": exp_move, "expected_heart_rate": {n: {"sample_count": v["sample_count"], "mean": v["mean"]} for n, v in exp_hr.items()}}


def run(seconds: int, out: Callable[[str], None], backend_factory: Callable = IsolatedBackend) -> dict:
    checks: dict = {}
    notes: list[str] = []
    backend = backend_factory()
    try:
        out(f"Starting an isolated backend on port {backend.port} (database {backend.db_path})…")
        start_backend(backend)
        base = backend.base_url
        hr_series = effort_rest_heart_rate_series(seconds)
        t0 = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=seconds + 30)
        records: list[RequestRecord] = []
        with httpx.Client(timeout=10.0) as c:
            for i, a in enumerate(ATHLETES):
                assert c.post(f"{base}/api/v1/athletes", json={"id": a, "name": f"Fictional Athlete {i + 1:02d}", "jersey_number": i + 1, "sport": "fictional", "team_id": "fictional-team"}).status_code == 201
            rec = c.post(f"{base}/api/v1/recordings", json={"name": "Fictional session", "athlete_ids": ATHLETES}).json()
            rec_id = rec["id"]
            for s in range(seconds):
                for i in range(len(ATHLETES)):
                    p = session_payloads(i, s, hr_series, t0 + timedelta(seconds=s))
                    if p is not None:
                        records += send(c, base, p)
            stopped = c.post(f"{base}/api/v1/recordings/{rec_id}/stop").json()
            checks["recording_stopped"] = stopped.get("status") == "stopped"
            checks["every_sample_accepted"] = all(r.outcome == "accepted" for r in records)
            # a period cannot be saved while a recording is active; a second recording stays separate
            rec2 = c.post(f"{base}/api/v1/recordings", json={"name": "Second session", "athlete_ids": [ATHLETES[0]]}).json()
            refused = c.post(f"{base}/api/v1/recordings/{rec2['id']}/athletes/{ATHLETES[0]}/periods", json={"label": "too early", "effort_start": iso(t0), "effort_end": iso(t0 + timedelta(seconds=5))})
            checks["saving_in_an_active_recording_is_409"] = refused.status_code == 409
            for s in range(3):
                records += send(c, base, session_payloads(0, s, hr_series, t0 + timedelta(seconds=seconds + s)))
            c.post(f"{base}/api/v1/recordings/{rec2['id']}/stop")
            # save the periods and download a snapshot of each
            plan = period_plan(t0)
            saved = []
            for item in plan:
                r = c.post(f"{base}/api/v1/recordings/{rec_id}/athletes/{ATHLETES[0]}/periods", json={"label": item["label"], "effort_start": iso(t0 + timedelta(seconds=item["start"])), "effort_end": iso(t0 + timedelta(seconds=item["end"]))})
                assert r.status_code == 201, r.text
                saved.append(r.json())
            other = c.post(f"{base}/api/v1/recordings/{rec_id}/athletes/{ATHLETES[1]}/periods", json={"label": "acceleration", "effort_start": iso(t0), "effort_end": iso(t0 + timedelta(seconds=10))}).json()
            in_second = c.post(f"{base}/api/v1/recordings/{rec2['id']}/athletes/{ATHLETES[0]}/periods", json={"label": "acceleration", "effort_start": iso(t0 + timedelta(seconds=seconds)), "effort_end": iso(t0 + timedelta(seconds=seconds + 2))}).json()
            checks["blank_label_and_invalid_period_are_422"] = (c.post(f"{base}/api/v1/recordings/{rec_id}/athletes/{ATHLETES[0]}/periods", json={"label": " ", "effort_start": iso(t0), "effort_end": iso(t0 + timedelta(seconds=5))}).status_code == 422
                                                                and c.post(f"{base}/api/v1/recordings/{rec_id}/athletes/{ATHLETES[0]}/periods", json={"label": "x", "effort_start": iso(t0 + timedelta(seconds=5)), "effort_end": iso(t0)}).status_code == 422)
            snapshots = {}
            for p in saved:
                r = c.get(f"{base}/api/v1/recordings/{rec_id}/athletes/{ATHLETES[0]}/periods/{p['id']}/review.json")
                assert r.status_code == 200, r.text
                snapshots[p["id"]] = {"text": r.text, "filename": r.headers.get("Content-Disposition"), "format": r.headers.get("X-Athletrix-Format")}
            out(f"Recorded {seconds} s for {len(ATHLETES)} athletes ({len(records)} samples accepted), saved {len(saved)} periods for the first athlete plus one for the second athlete and one in a second recording, downloaded {len(snapshots)} snapshots; restarting the backend on the same database…")
        backend.stop(); start_backend(backend)
        phys = sorted([r for r in records if r.athlete == ATHLETES[0] and r.stream == "physiological" and ts_of(r) < t0 + timedelta(seconds=seconds)], key=ts_of)
        track = sorted([r for r in records if r.athlete == ATHLETES[0] and r.stream == "tracking" and ts_of(r) < t0 + timedelta(seconds=seconds)], key=ts_of)
        per_period = []
        with httpx.Client(timeout=10.0) as c:
            listed = c.get(f"{base}/api/v1/recordings/{rec_id}/athletes/{ATHLETES[0]}/periods").json()
            checks["saved_periods_listed_after_restart_with_exact_bounds"] = sorted(listed, key=lambda p: p["id"]) == sorted(saved, key=lambda p: p["id"])
            checks["identical_labels_coexist_with_distinct_ids"] = sum(1 for p in listed if p["label"] == "second cycle effort") == 2 and len({p["id"] for p in listed}) == len(listed)
            other_listed = c.get(f"{base}/api/v1/recordings/{rec_id}/athletes/{ATHLETES[1]}/periods").json()
            second_listed = c.get(f"{base}/api/v1/recordings/{rec2['id']}/athletes/{ATHLETES[0]}/periods").json()
            checks["periods_separated_by_athlete_and_recording"] = ([p["id"] for p in other_listed] == [other["id"]] and [p["id"] for p in second_listed] == [in_second["id"]]
                                                                     and c.get(f"{base}/api/v1/recordings/{rec_id}/athletes/{ATHLETES[0]}/periods/{other['id']}").status_code == 404
                                                                     and c.get(f"{base}/api/v1/recordings/{rec2['id']}/athletes/{ATHLETES[0]}/periods/{saved[0]['id']}").status_code == 404)
            all_match = all_agree = True
            for p in saved:
                start, end = datetime.fromisoformat(p["effort_start"].replace("Z", "+00:00")), datetime.fromisoformat(p["effort_end"].replace("Z", "+00:00"))
                review = c.get(f"{base}/api/v1/recordings/{rec_id}/athletes/{ATHLETES[0]}/effort-review", params={"effort_start": p["effort_start"], "effort_end": p["effort_end"]}).json()
                ok, exp = review_matches_expectation(review, phys, track, start, end, 5.0)
                all_match = all_match and ok
                snap = json.loads(snapshots[p["id"]]["text"])
                agree = (snap["review"]["movement"] == review["movement"] and snap["review"]["heart_rate"] == review["heart_rate"] and snap["saved_period"] == p
                         and snap["calculation"]["distance_rules"]["high_speed_threshold_mps"] == 5.0 and snap["format"] == "athletrix-period-review"
                         and len(snap["samples"]["tracking"]) == review["movement"]["sample_count"]
                         and len(snap["samples"]["physiological"]) == sum(review["heart_rate"][n]["sample_count"] for n in ("before", "during", "after"))
                         and all(x["timestamp"] == r.payload["timestamp"] and x["speed_mps"] == r.payload["speed_mps"] and x["tracking_confidence"] == r.payload["tracking_confidence"] for x, r in zip(snap["samples"]["tracking"], [r for r in track if start <= ts_of(r) <= end])))
                all_agree = all_agree and agree
                per_period.append({"id": p["id"], "label": p["label"], "bounds": [p["effort_start"], p["effort_end"]], "matches_independent_expectation": ok, "snapshot_agrees_with_live_review": agree,
                                   "movement": {k: review["movement"].get(k) for k in ("sample_count", "estimated_distance_m", "high_speed_duration_seconds", "excluded_interval_count")},
                                   "heart_rate": {n: {"sample_count": review["heart_rate"][n]["sample_count"], "heart_rate_count": review["heart_rate"][n]["heart_rate_count"], "mean": review["heart_rate"][n]["heart_rate_mean_bpm"]} for n in ("before", "during", "after")}, **exp})
            checks["reopened_reviews_match_independent_expectations"] = all_match
            checks["snapshots_agree_with_the_live_reviews"] = all_agree
            gap = next(x for x in per_period if x["label"] == "gap")
            checks["empty_phase_and_missing_distance_stay_null"] = gap["movement"]["sample_count"] == 0 and gap["movement"]["estimated_distance_m"] is None and gap["heart_rate"]["during"]["sample_count"] == 0 and gap["heart_rate"]["during"]["mean"] is None
            checks["null_heart_rates_count_as_samples_not_measurements"] = any(x["heart_rate"]["during"]["sample_count"] > x["heart_rate"]["during"]["heart_rate_count"] for x in per_period)
            checks["low_confidence_points_exclude_intervals"] = any(x["movement"]["excluded_interval_count"] > 0 for x in per_period if x["label"] != "gap")
            checks["snapshot_filenames_and_format_header"] = all(v["filename"].startswith('attachment; filename="athletrix-period-review-') and v["format"] == "athletrix-period-review/1" for v in snapshots.values())
            notes.append("periods for the first athlete: " + "; ".join(f"{x['label']} [{x['bounds'][0]} to {x['bounds'][1]}]: {x['movement']['sample_count']} tracking samples, distance {x['movement']['estimated_distance_m'] if x['movement']['estimated_distance_m'] is None else round(x['movement']['estimated_distance_m'], 3)} m, high-speed {x['movement']['high_speed_duration_seconds'] if x['movement']['high_speed_duration_seconds'] is None else round(x['movement']['high_speed_duration_seconds'], 3)} s" for x in per_period))
        # a changed engine setting: a new review and a new snapshot reflect it; the saved bounds and the earlier snapshot do not change
        backend.stop(); start_backend(backend, {"HIGH_SPEED_THRESHOLD_MPS": CHANGED_THRESHOLD})
        with httpx.Client(timeout=10.0) as c:
            p = saved[1]   # "sustained": 6 m/s throughout, so the threshold change affects only the crossing intervals, but "acceleration" changes the crossing time
            p = next(x for x in saved if x["label"] == "acceleration")
            start, end = datetime.fromisoformat(p["effort_start"].replace("Z", "+00:00")), datetime.fromisoformat(p["effort_end"].replace("Z", "+00:00"))
            review = c.get(f"{base}/api/v1/recordings/{rec_id}/athletes/{ATHLETES[0]}/effort-review", params={"effort_start": p["effort_start"], "effort_end": p["effort_end"]}).json()
            ok, _ = review_matches_expectation(review, phys, track, start, end, float(CHANGED_THRESHOLD))
            earlier = json.loads(snapshots[p["id"]]["text"])
            r = c.get(f"{base}/api/v1/recordings/{rec_id}/athletes/{ATHLETES[0]}/periods/{p['id']}/review.json")
            later = json.loads(r.text)
            checks["changed_threshold_changes_a_new_review_and_matches_expectation"] = ok and review["movement"]["distance_rules"]["high_speed_threshold_mps"] == float(CHANGED_THRESHOLD) and review["movement"]["high_speed_duration_seconds"] != earlier["review"]["movement"]["high_speed_duration_seconds"]
            checks["new_snapshot_reflects_the_new_rules_and_the_same_saved_bounds"] = later["calculation"]["distance_rules"]["high_speed_threshold_mps"] == float(CHANGED_THRESHOLD) and later["saved_period"] == earlier["saved_period"] and later["review"]["movement"] == review["movement"] and later["samples"] == earlier["samples"]
            checks["earlier_snapshot_is_a_standalone_record"] = earlier["calculation"]["distance_rules"]["high_speed_threshold_mps"] == 5.0 and earlier["review"]["movement"]["high_speed_duration_seconds"] != later["review"]["movement"]["high_speed_duration_seconds"] and c.get(f"{base}/api/v1/recordings/{rec_id}/athletes/{ATHLETES[0]}/periods/{p['id']}").json() == p
            notes.append(f"'{p['label']}' high-speed time at 5.0 m/s: {earlier['review']['movement']['high_speed_duration_seconds']:.3f} s (earlier snapshot); at {CHANGED_THRESHOLD} m/s: {later['review']['movement']['high_speed_duration_seconds']:.3f} s (new review and snapshot); saved bounds unchanged")
    finally:
        backend.cleanup()
    return {"tool": "verify_period_workflow", "generated_at": datetime.now(UTC).isoformat(), "fictional_data": True,
            "config": {"seconds": seconds, "athletes": ATHLETES, "null_heart_rate_every_s": NULL_HR_EVERY, "low_confidence_every_s": LOW_CONF_EVERY, "gap_s": list(GAP), "changed_high_speed_threshold_mps": float(CHANGED_THRESHOLD)},
            "checks": checks, "per_period": per_period, "notes": notes, "passed": all(v is True for v in checks.values()),
            "limitations": ["A functional check on one machine with fictional data; not a performance, hardware or capacity test.",
                            "The independent expectations re-implement the documented rules; they cross-check the software, not any athlete."]}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--seconds", type=int, default=120, help="seconds of fictional session at 1 Hz (default 120; at least 100)")
    p.add_argument("--report", type=Path, default=None, help="JSON report path (default reports/period-workflow-<UTC time>.json, ignored by git)")
    return p


def main(argv: list[str] | None = None, *, out: Callable[[str], None] = print, backend_factory: Callable = IsolatedBackend) -> int:
    args = build_parser().parse_args(argv)
    if args.seconds < 100:
        out("seconds must be at least 100 so every planned period lies inside the session")
        return 2

    def interrupt(*_):
        raise KeyboardInterrupt
    previous = {s: signal.signal(s, interrupt) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        report = run(args.seconds, out, backend_factory)
    except KeyboardInterrupt:
        out("interrupted: the isolated backend started here was stopped and its temporary folder removed")
        return 130
    finally:
        for s, h in previous.items():
            signal.signal(s, h)
    path = args.report or (ROOT / "reports" / f"period-workflow-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    out("SAVED PERIOD WORKFLOW CHECK (fictional athletes, isolated backend)")
    for k, v in report["checks"].items():
        out(f"  {'PASS' if v is True else 'FAIL'}  {k}")
    for n in report["notes"]:
        out("  " + n)
    out(f"JSON report: {path}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
