"""End-to-end check of the portable recording workflow between two isolated engines, with fictional data only.

record -> stop -> save periods -> batch review -> export package -> dry run -> import into another engine -> restart
-> compare samples, roster, times, saved selections, id mapping, single/batch/CSV/JSON reviews -> change a setting.

WHAT IT DOES (normal development servers, databases and recordings are never touched)
  1. Source engine: an isolated backend (temporary database, free port, small rolling caps of 50/50 so the recording's
     independence from rolling retention is visible). Four fictional athletes are recorded with unequal rates
     (physiology 1 Hz, tracking 4 Hz) for --seconds: missing heart rates, real zero speeds, low-confidence tracking
     points, a gap for one athlete, a few samples delivered late with their original timestamps, and an equal-instant
     replacement. A hand-checkable athlete has a tiny known series. The recording is stopped and several periods are
     saved, including two with the same label.
  2. The package is exported over HTTP and saved in this run's temporary folder (never in the repository).
  3. A dry run against a missing destination and against an existing empty destination writes nothing (the missing
     file stays missing; the existing file stays byte-for-byte identical).
  4. A corrupted copy of the package and a forced failure in the middle of an import are both rejected with the
     destination unchanged (atomic rollback).
  5. The package is imported into the destination engine's database, the destination is started and then restarted,
     and everything is compared: normalised samples line for line, roster snapshot, recording times, saved labels and
     exact bounds, the provenance id mapping; the single effort review, batch review, CSV and detailed JSON of every
     period, under identical settings, against the source engine; and the hand-checkable period against values
     worked out by hand in this file (not by a production helper).
  6. The destination is restarted with another high-speed threshold: the reported rules and the affected figures
     change, while the imported samples and the saved selections stay identical.
  Writes a JSON report (reports/ is ignored by git). Exit 0 when every check passed.

WHAT IT IS NOT
  A software replay test with fictional values on one machine. It says nothing about real athlete physiology,
  wearable or camera accuracy, and it is not a performance test.

USAGE
    python scripts/verify_portable_workflow.py
    python scripts/verify_portable_workflow.py --seconds 120 --report reports/portable-workflow.json
"""

import argparse
import csv
import io
import json
import signal
import sys
import tempfile
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
for p in (str(ROOT), str(SCRIPTS)):
    if p not in sys.path:
        sys.path.insert(0, p)

import recording_package
from app.core.recording_package import FORMAT_VERSION as PACKAGE_VERSION  # noqa: E402
from compare_retention import close, independent_movement  # noqa: E402
from validate_team_scale import IsolatedBackend, RequestRecord  # noqa: E402
from verify_period_workflow import start_backend  # noqa: E402

ATHLETES = ["fictional-athlete-01", "fictional-athlete-02", "fictional-athlete-03"]
HAND = "fictional-hand-check"
TRACK_HZ = 4
GAP = (40, 46)              # athlete 02 sends nothing in [40, 46) s
LATE_SECOND = 10            # samples of this second are withheld and delivered just before the stop
# The hand-checkable athlete: tracking at 0, 1, 2 s with speeds 2, 4, 6 m/s (confidence 1), heart rate 100, null, 104.
# Period [0 s, 2 s] by hand, default rules (gap 3 s, confidence 0.5, threshold 5 m/s):
#   distance (2+4)/2*1 + (4+6)/2*1 = 8 m; speed crosses 5 m/s halfway through 1->2 s: 0.5 s and (5+6)/2*0.5 = 2.75 m
#   during [0, 2]: 3 samples, 2 measured, mean (100+104)/2 = 102 bpm; before [-10, 0): none; after (2, 32]: none
# With a threshold of 3 m/s: the crossing is halfway through 0->1 s (0.5 s) plus the whole 1->2 s interval = 1.5 s.
HAND_EXPECTED = {"distance_m": 8.0, "high_speed_s": 0.5, "high_speed_m": 2.75, "during": (3, 2, 102.0), "high_speed_s_at_3": 1.5}


def iso(ts: datetime) -> str:
    return ts.isoformat().replace("+00:00", "Z")


def payloads(i: int, tick: int, t0: datetime) -> list[tuple[str, dict]]:
    """Samples due at tracking tick `tick` (4 Hz) for athlete i: tracking every tick, physiology on whole seconds."""
    second, frac = divmod(tick, TRACK_HZ)
    if i == 1 and GAP[0] <= second < GAP[1]:
        return []
    ts = iso(t0 + timedelta(seconds=tick / TRACK_HZ))
    out = []
    if frac == 0:
        hr = None if second % 11 == 0 else round(80 + 40 * ((second % 30) / 30) + i, 1)
        out.append(("physiological", {"athlete_id": ATHLETES[i], "timestamp": ts, "source": "simulated", "device_id": f"fictional-wearable-{i + 1:02d}", "heart_rate_bpm": hr,
                                      "breathing_rate_bpm": 0.0 if second == 3 else None}))
    speed = 0.0 if (second % 20) >= 15 else round(1.0 + 0.25 * (tick % 24), 2)
    out.append(("tracking", {"athlete_id": ATHLETES[i], "timestamp": ts, "source": "simulated", "device_id": f"fictional-tracker-{i + 1:02d}",
                             "x_position": round(tick * 0.5, 2), "y_position": float(i), "speed_mps": speed, "acceleration_mps2": 0.0,
                             "tracking_confidence": 0.2 if tick % 37 == 0 else 1.0}))
    return out


def hand_payloads(t0: datetime) -> list[tuple[str, dict]]:
    out = []
    for s, (speed, hr) in enumerate(((2.0, 100.0), (4.0, None), (6.0, 104.0))):
        ts = iso(t0 + timedelta(seconds=s))
        out.append(("physiological", {"athlete_id": HAND, "timestamp": ts, "source": "manual", "device_id": "hand", "heart_rate_bpm": hr}))
        out.append(("tracking", {"athlete_id": HAND, "timestamp": ts, "source": "manual", "device_id": "hand", "x_position": 0.0, "y_position": 0.0, "speed_mps": speed, "acceleration_mps2": 0.0, "tracking_confidence": 1.0}))
    return out


def post(c: httpx.Client, base: str, stream: str, body: dict, records: list) -> None:
    r = c.post(f"{base}/api/v1/samples/{stream}", json=body)
    records.append(RequestRecord(body["athlete_id"], stream, 0, 0.0, 0.0, 0.0, "accepted" if r.status_code == 200 else "http_error", r.status_code, payload=body))


def jsonl_of(c: httpx.Client, base: str, rec_id: str, athlete: str) -> list[str]:
    r = c.get(f"{base}/api/v1/recordings/{rec_id}/athletes/{athlete}/recorded-samples.jsonl")
    return r.text.splitlines() if r.status_code == 200 else []


def strip_review(review: dict) -> dict:
    """A review without the parts that depend on which engine answered (capture time, recording reference)."""
    return {k: v for k, v in review.items() if k not in ("captured_at", "recording")}


def csv_rows(text: str) -> list[dict]:
    return [{k: v for k, v in row.items() if k not in ("recording_id", "period_id", "calculated_at")} for row in csv.DictReader(io.StringIO(text))]


def table_counts(path: Path) -> dict:
    import sqlite3
    if not path.exists():
        return {"exists": False}
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        out = {"exists": True}
        for t in ("recordings", "recording_athletes", "recording_physiological_samples", "recording_tracking_samples", "recording_periods", "recording_imports", "athletes", "physiological_samples", "tracking_samples"):
            try:
                out[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except Exception:  # noqa: BLE001
                out[t] = None
        return out
    finally:
        con.close()


def run(seconds: int, out: Callable[[str], None], workdir: Path, backend_factory: Callable = IsolatedBackend) -> dict:
    checks: dict = {}
    notes: list[str] = []
    source = backend_factory(retention_caps={"physiological": 50, "tracking": 50})
    dest = backend_factory()
    try:
        out(f"Source engine: isolated backend on port {source.port} with rolling caps 50/50 ({source.db_path})")
        start_backend(source)
        sb = source.base_url
        t0 = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=seconds + 60)
        records: list[RequestRecord] = []
        with httpx.Client(timeout=10.0) as c:
            for n, a in enumerate(ATHLETES + [HAND], start=1):
                assert c.post(f"{sb}/api/v1/athletes", json={"id": a, "name": f"Fictional {n:02d}", "jersey_number": n, "sport": ["football", "hockey", "rugby", "basketball"][n % 4], "team_id": "fictional-team"}).status_code == 201
            rec = c.post(f"{sb}/api/v1/recordings", json={"name": "Fictional portable session", "athlete_ids": ATHLETES + [HAND]}).json()
            rec_id = rec["id"]
            late = []
            for tick in range(seconds * TRACK_HZ):
                for i in range(len(ATHLETES)):
                    for stream, body in payloads(i, tick, t0):
                        if tick // TRACK_HZ == LATE_SECOND:
                            late.append((stream, body))
                        else:
                            post(c, sb, stream, body, records)
            for stream, body in hand_payloads(t0):
                post(c, sb, stream, body, records)
            for stream, body in late:                     # delayed arrivals: original timestamps, received later
                post(c, sb, stream, body, records)
            replaced = dict(next(b for s, b in payloads(0, 8, t0) if s == "physiological"), heart_rate_bpm=150.0)
            post(c, sb, "physiological", replaced, records)  # equal-instant replacement: the later arrival is kept
            stopped = c.post(f"{sb}/api/v1/recordings/{rec_id}/stop").json()
            checks["source_samples_accepted"] = all(r.outcome == "accepted" for r in records)
            checks["source_recording_stopped"] = stopped["status"] == "stopped"
            periods_plan = [(ATHLETES[0], "effort", 2, 14), (ATHLETES[0], "effort", 22, 34), (ATHLETES[0], "rest", 15, 20), (ATHLETES[1], "across the gap", 36, 50), (ATHLETES[2], "late arrivals", 8, 12), (HAND, "hand check", 0, 2)]
            for a, label, lo, hi in periods_plan:
                r = c.post(f"{sb}/api/v1/recordings/{rec_id}/athletes/{a}/periods", json={"label": label, "effort_start": iso(t0 + timedelta(seconds=lo)), "effort_end": iso(t0 + timedelta(seconds=hi))})
                assert r.status_code == 201, r.text
            src_detail = c.get(f"{sb}/api/v1/recordings/{rec_id}").json()
            checks["source_recording_independent_of_rolling_caps"] = (src_detail["recorded"][0]["tracking"]["sample_count"] > 50 and c.get(f"{sb}/api/v1/athletes/{ATHLETES[0]}/retained-data").json()["tracking"]["sample_count"] == 50)
            pkg_resp = c.get(f"{sb}/api/v1/recordings/{rec_id}/package.zip")
            pkg_path = workdir / "package.zip"
            pkg_path.write_bytes(pkg_resp.content)
            checks["package_downloaded"] = pkg_resp.status_code == 200 and pkg_resp.headers.get("X-Athletrix-Format") == f"athletrix-recording-package/{PACKAGE_VERSION}"
            src = {"jsonl": {a: jsonl_of(c, sb, rec_id, a) for a in ATHLETES + [HAND]},
                   "periods": {a: c.get(f"{sb}/api/v1/recordings/{rec_id}/athletes/{a}/periods").json() for a in ATHLETES + [HAND]},
                   "batch": {a: c.get(f"{sb}/api/v1/recordings/{rec_id}/athletes/{a}/period-reviews").json() for a in ATHLETES + [HAND]},
                   "csv": {a: c.get(f"{sb}/api/v1/recordings/{rec_id}/athletes/{a}/period-reviews.csv").text for a in ATHLETES + [HAND]},
                   "detail": src_detail}
            src["single"] = {p["id"]: c.get(f"{sb}/api/v1/recordings/{rec_id}/athletes/{p['athlete_id']}/effort-review", params={"effort_start": p["effort_start"], "effort_end": p["effort_end"]}).json() for ps in src["periods"].values() for p in ps}
            src["json"] = {p["id"]: json.loads(c.get(f"{sb}/api/v1/recordings/{rec_id}/athletes/{p['athlete_id']}/periods/{p['id']}/review.json").text) for ps in src["periods"].values() for p in ps}
        with zipfile.ZipFile(pkg_path) as zf:
            manifest = json.loads(zf.read("manifest.json"))
        notes.append(f"package: {pkg_path.stat().st_size} bytes, {manifest['counts']['physiological_samples']} physiological and {manifest['counts']['tracking_samples']} tracking samples, {manifest['counts']['periods']} periods, {manifest['counts']['athletes']} athletes")
        source.stop()

        # ---- dry runs, corrupt package, forced failure: the destination never changes ------------------------------------
        lines: list[str] = []
        missing = workdir / "never-created.db"
        code = recording_package.main(["import", str(pkg_path), "--database", str(missing), "--dry-run"], out=lines.append)
        checks["dry_run_against_a_missing_destination_creates_nothing"] = code == 0 and not missing.exists()
        from app.db import Database
        Database(dest.db_path).close()                       # an existing, empty destination database
        before_bytes = dest.db_path.read_bytes()
        code = recording_package.main(["import", str(pkg_path), "--database", str(dest.db_path), "--dry-run"], out=lines.append)
        checks["dry_run_leaves_an_existing_destination_byte_for_byte_unchanged"] = code == 0 and dest.db_path.read_bytes() == before_bytes
        corrupt = workdir / "corrupt.zip"
        with zipfile.ZipFile(pkg_path) as zin, zipfile.ZipFile(corrupt, "w") as zout:
            for n in zin.namelist():
                blob = zin.read(n)
                zout.writestr(n, blob.replace(b"fictional-tracker-01", b"fictional-tracker-99", 1) if n == "samples.jsonl" else blob)
        lines = []
        code = recording_package.main(["import", str(corrupt), "--database", str(dest.db_path)], out=lines.append)
        checks["corrupt_package_rejected_and_destination_unchanged"] = code == 1 and any("checksum" in l for l in lines) and dest.db_path.read_bytes() == before_bytes

        def fail_mid(stage):
            if stage.startswith("samples:") and stage.endswith(":tracking"):
                raise RuntimeError("forced failure in the middle of the sample import")
        lines = []
        code = recording_package.main(["import", str(pkg_path), "--database", str(dest.db_path)], out=lines.append, checkpoint=fail_mid)
        after_fail = table_counts(dest.db_path)
        checks["forced_mid_import_failure_rolls_back_completely"] = code == 1 and "rolled back completely" in lines[-1] and all((after_fail.get(t) or 0) == 0 for t in ("recordings", "recording_athletes", "recording_physiological_samples", "recording_tracking_samples", "recording_periods", "recording_imports"))

        # ---- import, start, restart, compare -------------------------------------------------------------------------------
        lines = []
        code = recording_package.main(["import", str(pkg_path), "--database", str(dest.db_path), "--json"], out=lines.append)
        imported = json.loads("\n".join(lines))
        checks["import_succeeded"] = code == 0 and imported["ok"] is True
        new_id = imported["recording_id"]
        lines = []
        checks["repeated_import_refused"] = recording_package.main(["import", str(pkg_path), "--database", str(dest.db_path)], out=lines.append) == 1 and any("REFUSED" in l for l in lines)
        out(f"Destination engine: imported as {new_id}; starting an isolated backend on port {dest.port} on that database, then restarting it…")
        start_backend(dest); dest.stop(); start_backend(dest)
        db = dest.base_url
        with httpx.Client(timeout=10.0) as c:
            detail = c.get(f"{db}/api/v1/recordings/{new_id}").json()
            checks["recording_times_and_roster_preserved"] = (detail["status"] == "stopped" and detail["started_at"] == src_detail["started_at"] and detail["stopped_at"] == src_detail["stopped_at"]
                                                              and detail["athletes"] == src_detail["athletes"] and detail["name"] == src_detail["name"])
            checks["provenance_maps_every_original_period_id"] = (detail["imported"]["source_recording_id"] == rec_id and sorted(detail["imported"]["period_id_map"]) == sorted(p["id"] for ps in src["periods"].values() for p in ps))
            checks["destination_registry_and_rolling_history_untouched"] = c.get(f"{db}/api/v1/athletes").json() == [] and c.get(f"{db}/api/v1/athletes/{ATHLETES[0]}/retained-data").json()["tracking"]["sample_count"] == 0
            checks["normalised_samples_identical_line_for_line"] = all(jsonl_of(c, db, new_id, a) == src["jsonl"][a] for a in ATHLETES + [HAND])
            id_map = detail["imported"]["period_id_map"]
            ok_periods = ok_single = ok_json = ok_batch = ok_csv = True
            dst_periods = {a: c.get(f"{db}/api/v1/recordings/{new_id}/athletes/{a}/periods").json() for a in ATHLETES + [HAND]}
            for a in ATHLETES + [HAND]:
                ok_periods = ok_periods and [(id_map[p["id"]], p["label"], p["effort_start"], p["effort_end"], p["created_at"]) for p in src["periods"][a]] == [(p["id"], p["label"], p["effort_start"], p["effort_end"], p["created_at"]) for p in dst_periods[a]]
                batch = c.get(f"{db}/api/v1/recordings/{new_id}/athletes/{a}/period-reviews").json()
                ok_batch = ok_batch and [strip_review(i["review"]) for i in batch["items"]] == [strip_review(i["review"]) for i in src["batch"][a]["items"]] and batch["distance_rules"] == src["batch"][a]["distance_rules"]
                ok_csv = ok_csv and csv_rows(c.get(f"{db}/api/v1/recordings/{new_id}/athletes/{a}/period-reviews.csv").text) == csv_rows(src["csv"][a])
                for p in dst_periods[a]:
                    orig = next(k for k, v in id_map.items() if v == p["id"])
                    single = c.get(f"{db}/api/v1/recordings/{new_id}/athletes/{a}/effort-review", params={"effort_start": p["effort_start"], "effort_end": p["effort_end"]}).json()
                    ok_single = ok_single and strip_review(single) == strip_review(src["single"][orig])
                    doc = json.loads(c.get(f"{db}/api/v1/recordings/{new_id}/athletes/{a}/periods/{p['id']}/review.json").text)
                    ok_json = ok_json and strip_review(doc["review"]) == strip_review(src["json"][orig]["review"]) and doc["samples"] == src["json"][orig]["samples"] and doc["calculation"]["distance_rules"] == src["json"][orig]["calculation"]["distance_rules"]
            checks["saved_labels_and_exact_bounds_preserved_under_new_ids"] = ok_periods and any(sum(1 for p in ps if p["label"] == "effort") == 2 for ps in dst_periods.values())
            checks["single_reviews_identical_to_source"] = ok_single
            checks["batch_reviews_identical_to_source"] = ok_batch
            checks["csv_identical_to_source_apart_from_ids_and_time"] = ok_csv
            checks["detailed_json_identical_to_source"] = ok_json
            # independent cross-check of every movement figure from the samples that were sent
            ok_ind = True
            for a in ATHLETES:
                inst = lambda text: datetime.fromisoformat(text.replace("Z", "+00:00"))  # noqa: E731  compare instants, never text
                dedup = {}
                for r in records:                                  # the last arrival per instant wins, as in the engine
                    if r.athlete == a and r.stream == "tracking":
                        dedup[inst(r.payload["timestamp"])] = r
                for item in c.get(f"{db}/api/v1/recordings/{new_id}/athletes/{a}/period-reviews").json()["items"]:
                    lo, hi = inst(item["period"]["effort_start"]), inst(item["period"]["effort_end"])
                    chosen = [r for t, r in sorted(dedup.items()) if lo <= t <= hi]
                    exp = independent_movement(chosen)
                    m = item["review"]["movement"]
                    ok_ind = ok_ind and m["sample_count"] == exp["sample_count"] and close(m["estimated_distance_m"], exp["estimated_distance_m"]) and close(m["high_speed_duration_seconds"], exp["high_speed_duration_seconds"])
            checks["movement_matches_independent_calculation"] = ok_ind
            hand = c.get(f"{db}/api/v1/recordings/{new_id}/athletes/{HAND}/period-reviews").json()["items"][0]["review"]
            hm, hd = hand["movement"], hand["heart_rate"]["during"]
            checks["hand_checked_period_matches_values_worked_out_by_hand"] = (close(hm["estimated_distance_m"], HAND_EXPECTED["distance_m"]) and close(hm["high_speed_duration_seconds"], HAND_EXPECTED["high_speed_s"])
                                                                               and close(hm["high_speed_distance_m"], HAND_EXPECTED["high_speed_m"]) and (hd["sample_count"], hd["heart_rate_count"], hd["heart_rate_mean_bpm"]) == HAND_EXPECTED["during"]
                                                                               and hand["heart_rate"]["before"]["sample_count"] == 0 and hand["heart_rate"]["after"]["sample_count"] == 0)
            gap = next(i for i in c.get(f"{db}/api/v1/recordings/{new_id}/athletes/{ATHLETES[1]}/period-reviews").json()["items"])
            checks["gap_is_visible_as_a_larger_inter_sample_gap"] = (gap["review"]["movement"]["max_inter_sample_gap_seconds"] or 0) >= GAP[1] - GAP[0]
            late = c.get(f"{db}/api/v1/recordings/{new_id}/athletes/{ATHLETES[2]}/period-reviews").json()["items"][0]["review"]
            checks["delayed_arrivals_recorded_at_their_own_timestamps"] = late["movement"]["sample_count"] == 4 * TRACK_HZ + 1
            phys0 = [json.loads(l)["sample"] for l in src["jsonl"][ATHLETES[0]] if json.loads(l)["type"] == "physiological"]
            checks["nulls_zeros_and_replacement_preserved"] = (any(s["heart_rate_bpm"] is None for s in phys0) and any(s.get("breathing_rate_bpm") == 0.0 for s in phys0)
                                                              and [s["heart_rate_bpm"] for s in phys0 if s["timestamp"] == replaced["timestamp"]] == [150.0])
            notes.append("hand-checked period on the destination: " + json.dumps({"distance_m": hm["estimated_distance_m"], "high_speed_s": hm["high_speed_duration_seconds"], "high_speed_m": hm["high_speed_distance_m"], "during": [hd["sample_count"], hd["heart_rate_count"], hd["heart_rate_mean_bpm"]]}))
            before_change = {"jsonl": {a: jsonl_of(c, db, new_id, a) for a in ATHLETES + [HAND]}, "periods": dst_periods}

        # ---- a changed destination setting --------------------------------------------------------------------------------
        dest.stop(); start_backend(dest, {"HIGH_SPEED_THRESHOLD_MPS": "3.0"})
        with httpx.Client(timeout=10.0) as c:
            batch = c.get(f"{db}/api/v1/recordings/{new_id}/athletes/{HAND}/period-reviews").json()
            row = next(csv.DictReader(io.StringIO(c.get(f"{db}/api/v1/recordings/{new_id}/athletes/{HAND}/period-reviews.csv").text)))
            pid = batch["items"][0]["period"]["id"]
            doc = json.loads(c.get(f"{db}/api/v1/recordings/{new_id}/athletes/{HAND}/periods/{pid}/review.json").text)
            checks["changed_setting_reported_and_applied"] = (batch["distance_rules"]["high_speed_threshold_mps"] == 3.0 and close(batch["items"][0]["review"]["movement"]["high_speed_duration_seconds"], HAND_EXPECTED["high_speed_s_at_3"])
                                                             and row["high_speed_threshold_mps"] == "3.0" and row["high_speed_duration_s"] == "1.5" and doc["calculation"]["distance_rules"]["high_speed_threshold_mps"] == 3.0)
            checks["imported_samples_and_saved_selections_unchanged_by_the_setting"] = (all(jsonl_of(c, db, new_id, a) == before_change["jsonl"][a] for a in ATHLETES + [HAND])
                                                                                     and all(c.get(f"{db}/api/v1/recordings/{new_id}/athletes/{a}/periods").json() == before_change["periods"][a] for a in ATHLETES + [HAND]))
            checks["earlier_source_snapshot_still_records_the_earlier_rule"] = src["json"][next(k for k, v in id_map.items() if v == pid)]["calculation"]["distance_rules"]["high_speed_threshold_mps"] == 5.0
    finally:
        source.cleanup()
        dest.cleanup()
    return {"tool": "verify_portable_workflow", "generated_at": datetime.now(UTC).isoformat(), "fictional_data": True,
            "config": {"seconds": seconds, "physiological_hz": 1, "tracking_hz": TRACK_HZ, "athletes": ATHLETES + [HAND], "gap_s": list(GAP), "late_second": LATE_SECOND, "source_rolling_caps": [50, 50]},
            "checks": checks, "notes": notes, "passed": all(v is True for v in checks.values()),
            "limitations": ["A software replay test with fictional values on one machine; it does not validate real athlete physiology, wearable or camera accuracy, or performance."]}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--seconds", type=int, default=90, help="seconds of fictional session (default 90; at least 60)")
    p.add_argument("--report", type=Path, default=None, help="JSON report path (default reports/portable-workflow-<UTC time>.json, ignored by git)")
    return p


def main(argv: list[str] | None = None, *, out: Callable[[str], None] = print, backend_factory: Callable = IsolatedBackend) -> int:
    args = build_parser().parse_args(argv)
    if args.seconds < 60:
        out("seconds must be at least 60 so every planned period lies inside the session")
        return 2

    def interrupt(*_):
        raise KeyboardInterrupt
    previous = {s: signal.signal(s, interrupt) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        with tempfile.TemporaryDirectory(prefix="athletrix-portable-") as tmp:   # the package and its corrupt copy live here only
            report = run(args.seconds, out, Path(tmp), backend_factory)
    except KeyboardInterrupt:
        out("interrupted: the isolated backends and temporary files of this run were removed")
        return 130
    finally:
        for s, h in previous.items():
            signal.signal(s, h)
    path = args.report or (ROOT / "reports" / f"portable-workflow-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    out("PORTABLE RECORDING WORKFLOW CHECK (fictional data, two isolated engines)")
    for k, v in report["checks"].items():
        out(f"  {'PASS' if v is True else 'FAIL'}  {k}")
    for n in report["notes"]:
        out("  " + n)
    out(f"JSON report: {path}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
