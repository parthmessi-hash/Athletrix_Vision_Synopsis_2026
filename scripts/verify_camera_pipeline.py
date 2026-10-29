"""End-to-end camera-adapter check against an isolated engine: synthetic video -> adapter -> engine -> recording -> package.

Needs OpenCV and NumPy (requirements-vision.txt). Fictional data only; the normal development servers, databases and
recordings are never touched (temporary folder, free localhost ports, everything removed at the end).

STEPS
  1. Run the synthetic camera check (scripts/camera_synthetic_check.py): video, calibration, synthetic marker
     tracking, explicit assignments, estimator errors against the analytic truth.
  2. Start an isolated engine, register two fictional athletes, start a recording whose adapter_configuration carries
     the adapter configuration, the calibration's SHA-256 and the time-mapping rule (operator-supplied, unverified).
  3. Send the tracking stream as a live-paced PLAYBACK (scripts/camera_to_samples.py send --anchor-utc now --speed 1):
     the first sample is timestamped when sending starts and later ones by their video time. This is playback of a
     generated file, not live camera capture. A fictional 1 Hz heart-rate feed for both athletes runs alongside.
  4. While sending, read each athlete's coherent live snapshot; afterwards stop the recording and compare: delivery
     accounting, recorded tracking samples against the exported ones (exact), tracking history, live snapshots (right
     athlete, source "simulated", the adapter's device id), movement summaries over known motion phases against the
     analytic path length, the recording's start snapshot, and a package export/import into a second isolated engine.
  Optional: --serve-frontend serves a copy of the dashboard pointed at this engine and --wait-before-send/--hold-after
  keep it up so the stream can be watched (Dashboard in the visible foreground tab).

USAGE
    python scripts/verify_camera_pipeline.py [--report PATH] [--serve-frontend "/path/to/Athletrix Fixed Website" --wait-before-send 20 --hold-after 60]
"""

import argparse
import json
import math
import shutil
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import camera_synthetic_check  # noqa: E402
import camera_to_samples  # noqa: E402
import recording_package  # noqa: E402
from camera_adapter.pipeline import AdapterConfig, file_sha256  # noqa: E402
from demo_launcher import StaticServer, prepare_frontend_copy  # noqa: E402
from simulate_athlete import heart_rate_at  # noqa: E402
from validate_team_scale import IsolatedBackend, free_port  # noqa: E402

ATHLETES = [{"id": "fictional-athlete-01", "name": "Fictional Camera Player 01", "jersey_number": 1, "sport": "multi-sport demo", "team_id": "fictional-camera-team"},
            {"id": "fictional-athlete-02", "name": "Fictional Camera Player 02", "jersey_number": 2, "sport": "multi-sport demo", "team_id": "fictional-camera-team"}]
COLOUR = {"fictional-athlete-01": "red", "fictional-athlete-02": "blue"}
# known motion phases (video seconds) and their analytic path length in metres (camera_adapter/synthetic_truth.py)
PHASES = [("fictional-athlete-01", 3.0, 12.0, 28.0, "accelerate, cruise 4 m/s (passing behind the occluder), decelerate"),
          ("fictional-athlete-01", 15.0, 21.0, 10.0, "accelerate, cruise 2 m/s, decelerate (far side)"),
          ("fictional-athlete-02", 2.0, 10.5, 21.0, "accelerate, cruise 3 m/s, decelerate"),
          ("fictional-athlete-02", 16.0, 25.0, 16.0, "the 4 m-radius curve at up to 2 m/s")]


def z(dt: datetime) -> str:
    return dt.isoformat(timespec="microseconds").replace("+00:00", "Z")


def run(workdir: Path, out: Callable[[str], None], frontend: Path | None, wait_before: float, hold_after: float) -> dict:
    checks: dict[str, dict] = {}

    def check(name, expected, actual, ok=None):
        passed = (expected == actual) if ok is None else bool(ok)
        checks[name] = {"expected": expected, "actual": actual, "passed": passed}

    synthetic = camera_synthetic_check.run(workdir, out)
    config_path = camera_synthetic_check.EXAMPLE_CONFIG
    config = AdapterConfig.from_doc(json.loads(config_path.read_text()))
    cal_sha = file_sha256(workdir / "calibration.json")
    fport = free_port() if frontend else None
    engine = IsolatedBackend(cors_origins=[f"http://localhost:{fport}", f"http://127.0.0.1:{fport}"] if fport else None)
    dest = IsolatedBackend()
    server = None
    try:
        engine.start()
        b = engine.base_url
        if frontend:
            prepare_frontend_copy(frontend, workdir / "frontend", b)
            server = StaticServer(workdir / "frontend", fport)
            server.start()
            out(f"DASHBOARD (isolated copy): http://localhost:{fport}/#demo  · engine {b}")
        with httpx.Client(timeout=30) as c:
            for a in ATHLETES:
                assert c.post(f"{b}/api/v1/athletes", json=a).status_code == 201
            metadata = config.as_recording_metadata(cal_sha, None)
            metadata["time_mapping"] = "live-paced playback: first sample timestamped when sending starts; later samples + video time; not camera capture time"
            rec = c.post(f"{b}/api/v1/recordings", json={"name": "synthetic camera adapter check", "athlete_ids": [a["id"] for a in ATHLETES],
                                                          "adapter_configuration": {"note": "synthetic marker tracking (not person detection); fictional data; calibration and estimator settings in metadata",
                                                                                    "metadata": metadata}})
            assert rec.status_code == 201, rec.text
            rec_id = rec.json()["id"]
        if wait_before:
            out(f"waiting {wait_before:g} s before sending (open the dashboard now)")
            time.sleep(wait_before)

        stop = threading.Event()
        hr_posts = {"ok": 0, "failed": 0}
        lives: list[dict] = []

        def heart_rate_feed():
            with httpx.Client(timeout=10) as c:
                start, n = time.monotonic(), 0
                while not stop.is_set():
                    now = datetime.now(UTC)
                    for i, a in enumerate(ATHLETES):
                        r = c.post(f"{b}/api/v1/samples/physiological", json={"athlete_id": a["id"], "timestamp": z(now), "source": "simulated", "device_id": f"fictional-wearable-0{i + 1}",
                                                                               "heart_rate_bpm": heart_rate_at((n + 5 * i) % 40, 40.0)})
                        hr_posts["ok" if r.status_code == 200 else "failed"] += 1
                    n += 1
                    stop.wait(max(0.0, start + n - time.monotonic()))

        def live_reads():
            with httpx.Client(timeout=10) as c:
                for when in (6.0, 18.0):
                    if stop.wait(when if not lives else when - 6.0):
                        return
                    for a in ATHLETES:
                        lives.append(c.get(f"{b}/api/v1/athletes/{a['id']}/live").json())

        feeder, reader = threading.Thread(target=heart_rate_feed), threading.Thread(target=live_reads)
        feeder.start(); reader.start()
        send_lines: list[str] = []
        code = camera_to_samples.main(["send", str(workdir / "observations.jsonl"), "--calibration", str(workdir / "calibration.json"), "--config", str(config_path),
                                       "--assignments", str(workdir / "assignments.json"), "--anchor-utc", "now", "--speed", "1", "--url", b,
                                       "--out", str(workdir / "sent.jsonl"), "--log", str(workdir / "sent-log.json")], out=send_lines.append)
        stop.set(); feeder.join(); reader.join()
        sent = [json.loads(x)["sample"] for x in (workdir / "sent.jsonl").read_text().splitlines()]
        log = json.loads((workdir / "sent-log.json").read_text())
        anchor = datetime.fromisoformat(log["time_mapping"]["anchor_utc"].replace("Z", "+00:00"))
        check("send completed: every exported sample submitted, none failed or unsent", [0, f"Result: {len(sent)} submitted, 0 failed, 0 not sent (of {len(sent)})."],
              [code, next((x for x in send_lines if x.startswith("Result:")), None)])
        time.sleep(1.0)
        with httpx.Client(timeout=30) as c:
            c.post(f"{b}/api/v1/recordings/{rec_id}/stop")
            detail = c.get(f"{b}/api/v1/recordings/{rec_id}").json()
            per = {e["athlete"]["id"]: e for e in detail["recorded"]}
            expected = {a["id"]: [s for s in sent if s["athlete_id"] == a["id"]] for a in ATHLETES}
            check("recorded tracking counts equal the samples sent per athlete", {a: len(v) for a, v in expected.items()}, {a: per[a]["tracking"]["sample_count"] for a in expected})
            check("heart-rate feed recorded alongside (fictional, 1 Hz)", True, all(per[a]["physiological"]["sample_count"] > 10 for a in expected) and hr_posts["failed"] == 0)
            recorded = {a: [json.loads(x)["sample"] for x in c.get(f"{b}/api/v1/recordings/{rec_id}/athletes/{a}/recorded-samples.jsonl").text.splitlines() if json.loads(x)["type"] == "tracking"] for a in expected}
            def norm(s):   # the same instant may be written with or without a zero fraction
                return {**s, "timestamp": datetime.fromisoformat(s["timestamp"].replace("Z", "+00:00"))}
            same = all([norm(s) for s in recorded[a]] == [norm(s) for s in expected[a]] for a in expected)
            check("recorded tracking samples identical to the exported ones (no athlete substituted, nothing added)", True, same)
            hist = {a: c.get(f"{b}/api/v1/athletes/{a}/tracking/history?limit=300").json() for a in expected}
            check("rolling tracking history holds the newest samples sent", True, all([h["timestamp"] for h in hist[a]][-50:] == [s["timestamp"] for s in recorded[a]][-50:] for a in expected))
            live_ok = [l for l in lives if l.get("tracking")]
            check("live snapshots during sending name the right athlete, source simulated and the adapter's device id", True,
                  bool(live_ok) and all(l["tracking"]["athlete_id"] == l["athlete_id"] and l["tracking"]["source"] == "simulated" and l["tracking"]["device_id"] == config.device_id for l in live_ok))
            snap = detail["configuration_at_start"]["snapshot"]["operator_supplied"]
            check("recording start snapshot carries the adapter configuration as unverified operator information", [cal_sha[:64], False, config.estimator.window_s],
                  [snap["metadata"]["calibration_sha256"], snap["verified_by_engine"], snap["metadata"]["estimator.window_s"]])
            phases = []
            for a, t0, t1, truth_m, what in PHASES:
                r = c.get(f"{b}/api/v1/recordings/{rec_id}/athletes/{a}/effort-review", params={"effort_start": z(anchor + timedelta(seconds=t0)), "effort_end": z(anchor + timedelta(seconds=t1))}).json()
                mv = r["movement"]
                phases.append({"athlete_id": a, "video_s": [t0, t1], "phase": what, "truth_path_m": truth_m, "engine_distance_m": mv["estimated_distance_m"],
                               "difference_m": None if mv["estimated_distance_m"] is None else round(mv["estimated_distance_m"] - truth_m, 3),
                               "samples": mv["sample_count"], "peak_speed_mps": mv["speed_peak_mps"], "excluded_intervals": mv["excluded_interval_count"],
                               "max_gap_s": mv["max_inter_sample_gap_seconds"]})
            check("engine movement summaries computed for every known phase", True, all(p["engine_distance_m"] is not None for p in phases))
            package = c.get(f"{b}/api/v1/recordings/{rec_id}/package.zip").content
        engine.stop()
        (workdir / "camera.zip").write_bytes(package)
        lines: list[str] = []
        code = recording_package.main(["import", str(workdir / "camera.zip"), "--database", str(dest.db_path), "--json"], out=lines.append)
        new_id = json.loads("\n".join(lines))["recording_id"]
        dest.start()
        with httpx.Client(timeout=30) as c:
            ddetail = c.get(f"{dest.base_url}/api/v1/recordings/{new_id}").json()
            drec = {a: [json.loads(x)["sample"] for x in c.get(f"{dest.base_url}/api/v1/recordings/{new_id}/athletes/{a}/recorded-samples.jsonl").text.splitlines() if json.loads(x)["type"] == "tracking"] for a in recorded}
        check("package import keeps every tracking sample and the start snapshot (origin source_engine)", [0, True, "source_engine", snap],
              [code, drec == recorded, ddetail["configuration_at_start"]["origin"], ddetail["configuration_at_start"]["snapshot"]["operator_supplied"]])
        if hold_after and server:
            out(f"holding the engine and dashboard for {hold_after:g} s")
            engine.start()
            time.sleep(hold_after)
    finally:
        if server:
            server.stop()
        engine.cleanup()
        dest.cleanup()
    return {"tool": "verify_camera_pipeline", "generated_at": datetime.now(UTC).isoformat(), "fictional_data": True,
            "synthetic": {k: synthetic[k] for k in ("calibration", "observations", "samples", "skipped", "resets", "unassigned_tracks", "errors", "identity_swaps", "checks", "passed")},
            "time_mapping": log["time_mapping"], "delivery": {"sent": len(sent), "send_output": send_lines[-3:], "heart_rate_posts": hr_posts},
            "movement_phases": phases, "checks": checks, "passed": synthetic["passed"] and all(v["passed"] for v in checks.values()),
            "limitations": ["Synthetic markers and a virtual camera: nothing here measures people, real footage or a real camera's timing.",
                            "Live-paced playback of a generated file; timestamps follow the stated anchor, not capture time.",
                            "The engine's distance integrates the average of the two endpoint speeds over each interval up to DISTANCE_MAX_GAP_SECONDS (3 s), so it spans such gaps by assuming speed changed linearly; it never uses x/y positions, and the adapter itself never invents samples."]}


def main(argv: list[str] | None = None, *, out: Callable[[str], None] = print) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--report", type=Path)
    p.add_argument("--serve-frontend", type=Path, help="serve a copy of this dashboard folder pointed at the isolated engine")
    p.add_argument("--wait-before-send", type=float, default=0.0)
    p.add_argument("--hold-after", type=float, default=0.0)
    args = p.parse_args(argv)
    try:
        import cv2  # noqa: F401, PLC0415
    except ImportError:
        out("This check needs OpenCV and NumPy: pip install -r requirements-vision.txt")
        return 2
    with tempfile.TemporaryDirectory(prefix="athletrix-camera-e2e-") as tmp:
        report = run(Path(tmp), out, args.serve_frontend, args.wait_before_send, args.hold_after)
    path = args.report or ROOT / "reports" / f"camera-pipeline-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    out("CAMERA ADAPTER -> ENGINE CHECK (synthetic marker tracking; fictional data; isolated engines)")
    for k, v in report["synthetic"]["checks"].items():
        out(f"  {'PASS' if v else 'FAIL'}  synthetic: {k}")
    for k, v in report["checks"].items():
        out(f"  {'PASS' if v['passed'] else 'FAIL'}  {k}" + ("" if v["passed"] else f"  (expected {v['expected']!r}, got {v['actual']!r})"))
    for ph in report["movement_phases"]:
        out(f"  {ph['athlete_id']} {ph['video_s'][0]:g}-{ph['video_s'][1]:g} s ({ph['phase']}): engine {ph['engine_distance_m']} m vs path {ph['truth_path_m']} m "
            f"(diff {ph['difference_m']} m; {ph['samples']} samples, {ph['excluded_intervals']} excluded intervals, largest gap {ph['max_gap_s']} s)")
    out(f"JSON report: {path}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
