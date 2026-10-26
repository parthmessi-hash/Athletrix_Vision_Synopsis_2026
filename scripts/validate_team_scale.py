"""Repeatable local validation of the engine with many fictional athletes sending samples concurrently.

WHAT IT DOES
  1. Starts its own isolated backend: a fresh temporary SQLite database and a free localhost port. The normal
     development backend and database are never touched, and only the process this tool started is stopped.
  2. Sends, for every fictional athlete, one physiological and one tracking sample per tick at the requested
     rate (default 1 Hz) for the requested duration (default 15 s), scheduled on a monotonic clock. Every
     request records when it was due, when it was actually sent, how long the round trip took and how it
     ended. Failures are counted, never retried, and never replaced by invented successes.
  3. Reads back what the backend retained and checks it against the samples that were accepted: per-athlete
     counts (with the newest-300 cap), latest timestamps, athlete and device separation, both streams in one
     window, and a recorded summary over the run against the accepted samples. A request that timed out has
     an unknown outcome (the backend may or may not have stored it), so the affected athlete's checks are
     reported as unknown rather than forced to pass or fail.
  4. Writes a JSON report (kept out of git by default: reports/ is ignored) and prints a readable summary.

WHAT IT IS NOT
  Not a hardware test, not proof of production capacity, not a sustained match-length soak. A 15 s run on one
  machine shows how this software behaves under an 11-20 athlete load right now; the numbers are observations
  with the machine context noted, not thresholds, and no pass mark is invented for latency.

USAGE
    python scripts/validate_team_scale.py                      # 20 athletes, ~15 s, 1 Hz
    python scripts/validate_team_scale.py --athletes 11 --duration 15
    python scripts/validate_team_scale.py --report reports/my-run.json
"""

import argparse
import json
import math
import os
import platform
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from simulate_athlete import heart_rate_at, motion_at  # noqa: E402  (deterministic fictional profiles)

DEFAULT_ATHLETES = 20
DEFAULT_DURATION_S = 15.0
DEFAULT_RATE_HZ = 1.0
DEFAULT_TIMEOUT_S = 5.0
RETENTION_CAP = 300
STREAMS = ("physiological", "tracking")
SOURCE = "simulated"


# ---- fictional athletes and samples -----------------------------------------------------------------------

def athlete_id(i: int) -> str:
    return f"fictional-athlete-{i + 1:02d}"


def device_id(i: int, stream: str) -> str:
    return f"fictional-{'wearable' if stream == 'physiological' else 'tracker'}-{i + 1:02d}"


def sample_payloads(i: int, tick: int, duration_s: float, rate_hz: float, timestamp: datetime) -> dict[str, dict]:
    """Both samples for athlete i at a tick: the simulator's sine profile, phase-shifted per athlete so values differ."""
    elapsed = tick / rate_hz + i  # each athlete starts at a different point of the same deterministic profile
    hr = heart_rate_at(elapsed % max(duration_s, 1.0), max(duration_s, 1.0))
    speed, x_position, acceleration = motion_at(elapsed % max(duration_s, 1.0), max(duration_s, 1.0))
    ts = timestamp.isoformat()
    return {
        "physiological": {"athlete_id": athlete_id(i), "timestamp": ts, "source": SOURCE, "device_id": device_id(i, "physiological"), "heart_rate_bpm": round(hr, 3)},
        "tracking": {"athlete_id": athlete_id(i), "timestamp": ts, "source": SOURCE, "device_id": device_id(i, "tracking"),
                     "x_position": round(x_position, 3), "y_position": float(i), "speed_mps": round(speed, 3), "acceleration_mps2": round(acceleration, 3), "tracking_confidence": 1.0},
    }


# ---- request records --------------------------------------------------------------------------------------------

@dataclass
class RequestRecord:
    athlete: str
    stream: str
    tick: int
    scheduled_s: float          # due time on the run's monotonic clock, seconds from start
    sent_s: float | None        # when the request was actually issued; None if never sent
    latency_s: float | None     # round trip measured in this process around client.post (connect + send + backend + response)
    outcome: str                # accepted | http_error | timeout | connection_error | unsent
    status: int | None
    detail: str = ""
    payload: dict = field(default_factory=dict)

    @property
    def lateness_s(self) -> float | None:
        return None if self.sent_s is None else self.sent_s - self.scheduled_s


def send_one(client: httpx.Client, base_url: str, stream: str, payload: dict, clock: Callable[[], float]) -> tuple[float, str, int | None, str]:
    """POST one sample; returns (latency, outcome, status, detail). Never retries."""
    t0 = clock()
    try:
        r = client.post(f"{base_url}/api/v1/samples/{stream}", json=payload)
    except httpx.TimeoutException as exc:
        return clock() - t0, "timeout", None, f"{type(exc).__name__}: acceptance unknown"
    except httpx.HTTPError as exc:
        return clock() - t0, "connection_error", None, type(exc).__name__
    latency = clock() - t0
    if r.status_code == 200:
        return latency, "accepted", 200, ""
    return latency, "http_error", r.status_code, r.text[:200]


# ---- the run ------------------------------------------------------------------------------------------------------

@dataclass
class RunConfig:
    athletes: int = DEFAULT_ATHLETES
    duration_s: float = DEFAULT_DURATION_S
    rate_hz: float = DEFAULT_RATE_HZ
    timeout_s: float = DEFAULT_TIMEOUT_S
    workers: int = 8
    grace_s: float = 10.0       # after the last tick, how long to wait for outstanding requests before marking the rest unsent

    @property
    def ticks(self) -> int:
        return max(1, int(round(self.duration_s * self.rate_hz)))


def run_load(
    base_url: str,
    cfg: RunConfig,
    *,
    make_client: Callable[[], httpx.Client] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    start_utc: datetime | None = None,
) -> tuple[list[RequestRecord], float, datetime]:
    """Send every scheduled sample; return (records, actual run duration in seconds, start time in UTC).

    Ticks are due at start + tick / rate on the monotonic clock. A tick's requests are handed to a small thread pool
    when due (never earlier); lateness is how long after its due time a request was actually issued. Requests that
    could not be issued before the deadline (last due time + grace) are recorded as unsent, never as successes.
    """
    start_utc = start_utc or datetime.now(UTC)
    records: list[RequestRecord] = []
    lock = threading.Lock()
    make_client = make_client or (lambda: httpx.Client(timeout=cfg.timeout_s))
    client = make_client()
    start = clock()
    deadline = start + cfg.ticks / cfg.rate_hz + cfg.grace_s

    def job(i: int, tick: int, due: float) -> None:
        stamp = start_utc + timedelta(seconds=tick / cfg.rate_hz)
        payloads = sample_payloads(i, tick, cfg.duration_s, cfg.rate_hz, stamp)
        for stream in STREAMS:
            if clock() > deadline:
                rec = RequestRecord(athlete_id(i), stream, tick, due, None, None, "unsent", None, "deadline passed before this request was issued", payloads[stream])
            else:
                sent = clock()
                latency, outcome, status, detail = send_one(client, base_url, stream, payloads[stream], clock)
                rec = RequestRecord(athlete_id(i), stream, tick, due, sent - start, latency, outcome, status, detail, payloads[stream])
            with lock:
                records.append(rec)

    try:
        with ThreadPoolExecutor(max_workers=max(1, cfg.workers)) as pool:
            futures = []
            for tick in range(cfg.ticks):
                due = start + tick / cfg.rate_hz
                wait = due - clock()
                if wait > 0:
                    sleep(wait)
                for i in range(cfg.athletes):
                    futures.append(pool.submit(job, i, tick, due - start))
            for f in futures:
                remaining = deadline - clock()
                try:
                    f.result(timeout=max(0.0, remaining))
                except Exception:  # noqa: BLE001  a worker that did not finish in time: its requests are recorded by the job as unsent or not at all
                    pass
    finally:
        client.close()
    actual = clock() - start
    # any (athlete, tick, stream) with no record at all was never issued
    seen = {(r.athlete, r.tick, r.stream) for r in records}
    for tick in range(cfg.ticks):
        for i in range(cfg.athletes):
            for stream in STREAMS:
                if (athlete_id(i), tick, stream) not in seen:
                    records.append(RequestRecord(athlete_id(i), stream, tick, tick / cfg.rate_hz, None, None, "unsent", None, "never issued", {}))
    records.sort(key=lambda r: (r.tick, r.athlete, r.stream))
    return records, actual, start_utc


# ---- statistics ---------------------------------------------------------------------------------------------------

def percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile; None when there are no values."""
    if not values:
        return None
    ordered = sorted(values)
    k = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[min(k, len(ordered)) - 1]


def summarize(values: list[float]) -> dict:
    return {"count": len(values), "p50": percentile(values, 50), "p95": percentile(values, 95), "max": max(values) if values else None, "mean": statistics.fmean(values) if values else None}


def count_outcomes(records: list[RequestRecord]) -> dict:
    out: dict = {"expected": len(records), "attempted": 0, "accepted": 0, "failed": 0, "unsent": 0, "http_error": 0, "timeout": 0, "connection_error": 0, "by_stream": {}, "http_status_counts": {}}
    for s in STREAMS:
        out["by_stream"][s] = {"expected": 0, "attempted": 0, "accepted": 0, "failed": 0, "unsent": 0, "timeout": 0}
    for r in records:
        b = out["by_stream"][r.stream]
        b["expected"] += 1
        if r.outcome == "unsent":
            out["unsent"] += 1; b["unsent"] += 1
            continue
        out["attempted"] += 1; b["attempted"] += 1
        if r.outcome == "accepted":
            out["accepted"] += 1; b["accepted"] += 1
        else:
            out["failed"] += 1; b["failed"] += 1
            out[r.outcome] += 1
            if r.outcome == "timeout":
                b["timeout"] += 1
            if r.status is not None:
                out["http_status_counts"][str(r.status)] = out["http_status_counts"].get(str(r.status), 0) + 1
    return out


# ---- verification against the backend --------------------------------------------------------------------------------

UNCERTAIN_WHEN_TIMED_OUT = {"window_has_both_streams", "summary_counts", "summary_heart_rate_mean", "summary_speed_peak", "summary_sources_simulated"}


def expected_retained(records: list[RequestRecord], athlete: str, stream: str) -> list[RequestRecord]:
    """Accepted samples for the athlete and stream, newest 300 by timestamp (what the backend should retain)."""
    acc = [r for r in records if r.athlete == athlete and r.stream == stream and r.outcome == "accepted"]
    acc.sort(key=lambda r: r.payload["timestamp"])
    return acc[-RETENTION_CAP:]


def verify(client: httpx.Client, base_url: str, records: list[RequestRecord], cfg: RunConfig, start_utc: datetime) -> dict:
    """Read the backend back and compare with the accepted samples. Athletes with a timed-out request are 'unknown'."""
    per_athlete = []
    overall = {"checked": 0, "passed": 0, "failed": 0, "unknown": 0}
    seen_devices: dict[str, set[str]] = {}
    for i in range(cfg.athletes):
        a = athlete_id(i)
        timed_out = any(r.athlete == a and r.outcome == "timeout" for r in records)
        checks: dict[str, bool | None] = {}
        notes: list[str] = []
        try:
            retained = client.get(f"{base_url}/api/v1/athletes/{a}/retained-data").json()
            exp = {s: expected_retained(records, a, s) for s in STREAMS}
            for s in STREAMS:
                got = retained[s]["sample_count"]
                checks[f"{s}_retained_count"] = got == len(exp[s])
                if exp[s]:
                    latest = client.get(f"{base_url}/api/v1/athletes/{a}/{s}/latest").json()
                    checks[f"{s}_latest_timestamp"] = datetime.fromisoformat(latest["timestamp"].replace("Z", "+00:00")) == datetime.fromisoformat(exp[s][-1].payload["timestamp"])
                    checks[f"{s}_latest_athlete_and_device"] = latest["athlete_id"] == a and latest["device_id"] == device_id(i, s)
                    seen_devices.setdefault(a, set()).add(latest["device_id"])
                    history = client.get(f"{base_url}/api/v1/athletes/{a}/{s}/history?limit={RETENTION_CAP}").json()
                    checks[f"{s}_history_only_this_athlete"] = all(h["athlete_id"] == a for h in history)
            if exp["physiological"] or exp["tracking"]:
                last_ts = max((r.payload["timestamp"] for s in STREAMS for r in exp[s]), key=lambda t: datetime.fromisoformat(t))
                window_s = min(300, max(1, int(cfg.ticks / cfg.rate_hz) + 1))
                win = client.get(f"{base_url}/api/v1/athletes/{a}/window", params={"seconds": window_s, "end": last_ts}).json()
                checks["window_has_both_streams"] = len(win["physiological_samples"]) == len(exp["physiological"]) and len(win["tracking_samples"]) == len(exp["tracking"])
                summary = client.get(f"{base_url}/api/v1/athletes/{a}/summary", params={"seconds": window_s, "end": last_ts}).json()
                p, t = summary["physiological"], summary["tracking"]
                hrs = [r.payload["heart_rate_bpm"] for r in exp["physiological"]]
                speeds = [r.payload["speed_mps"] for r in exp["tracking"]]
                checks["summary_counts"] = p["sample_count"] == len(hrs) and p["heart_rate_count"] == len(hrs) and t["sample_count"] == len(speeds)
                checks["summary_heart_rate_mean"] = (p["heart_rate_mean_bpm"] is None and not hrs) or (hrs and abs(p["heart_rate_mean_bpm"] - statistics.fmean(hrs)) < 1e-6)
                checks["summary_speed_peak"] = (t["speed_peak_mps"] is None and not speeds) or (speeds and abs(t["speed_peak_mps"] - max(speeds)) < 1e-9)
                checks["summary_sources_simulated"] = p["sources"] == ({"reported": {"simulated": len(hrs)}, "not_reported": 0} if hrs else {"reported": {}, "not_reported": 0})
        except Exception as exc:  # noqa: BLE001  a read failure is a verification failure, reported as such
            notes.append(f"verification read failed: {type(exc).__name__}: {exc}")
            checks["reads"] = False
        if timed_out:
            # a timed-out request may or may not have been stored, so every check that depends on exactly which samples
            # were accepted is unknown when it does not match; identity checks (athlete and device ids) stay strict
            notes.append("at least one request timed out: acceptance unknown, so sample-set checks that do not match are reported as unknown")
            for k, v in list(checks.items()):
                if v is False and (k.endswith("_retained_count") or k.endswith("_latest_timestamp") or k in UNCERTAIN_WHEN_TIMED_OUT):
                    checks[k] = None
        status = "unknown" if any(v is None for v in checks.values()) and not any(v is False for v in checks.values()) else ("failed" if any(v is False for v in checks.values()) else "passed")
        overall["checked"] += 1; overall[status] += 1
        per_athlete.append({"athlete_id": a, "status": status, "checks": checks, "notes": notes})
    # athletes and devices separate: every athlete's latest samples name its own ids (checked above) and no device id is shared
    all_devices = [d for ds in seen_devices.values() for d in ds]
    overall["device_ids_distinct"] = len(all_devices) == len(set(all_devices))
    overall["athletes_distinct"] = len({p["athlete_id"] for p in per_athlete}) == cfg.athletes
    return {"overall": overall, "per_athlete": per_athlete}


# ---- isolated backend process ------------------------------------------------------------------------------------------

def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class IsolatedBackend:
    """Starts uvicorn for app.main:app on a free port with a fresh temporary database; stops exactly that process."""

    def __init__(self, port: int | None = None, log_level: str = "warning") -> None:
        self.port = port or free_port()
        self.tmp = tempfile.TemporaryDirectory(prefix="athletrix-team-scale-")
        self.db_path = Path(self.tmp.name) / "isolated.db"
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.cmd = [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(self.port), "--log-level", log_level]
        self.proc: subprocess.Popen | None = None
        self.log_path = Path(self.tmp.name) / "backend.log"

    def start(self, ready_timeout_s: float = 20.0) -> None:
        env = {**os.environ, "DATABASE_PATH": str(self.db_path), "CORS_ORIGINS": "http://localhost:3000"}
        self.proc = subprocess.Popen(self.cmd, cwd=str(ROOT), env=env, stdout=open(self.log_path, "w"), stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + ready_timeout_s
        with httpx.Client(timeout=1.0) as c:
            while time.monotonic() < deadline:
                if self.proc.poll() is not None:
                    raise RuntimeError(f"backend exited early with code {self.proc.returncode}; see {self.log_path}")
                try:
                    if c.get(f"{self.base_url}/api/v1/health").status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
        self.stop()
        raise RuntimeError(f"backend on port {self.port} did not become ready within {ready_timeout_s} s; see {self.log_path}")

    def stop(self) -> None:
        p = self.proc
        if p is None:
            return
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout=5)
        self.proc = None

    def cleanup(self) -> None:
        self.stop()
        self.tmp.cleanup()


# ---- report ------------------------------------------------------------------------------------------------------------

def machine_context() -> dict:
    return {"platform": platform.platform(), "python": platform.python_version(), "machine": platform.machine(), "cpu_count": os.cpu_count()}


def build_report(cfg: RunConfig, backend: dict, records: list[RequestRecord], actual_s: float, start_utc: datetime, verification: dict) -> dict:
    sent = [r for r in records if r.sent_s is not None]
    latencies = [r.latency_s for r in sent if r.latency_s is not None]
    lateness = [r.lateness_s for r in sent if r.lateness_s is not None]
    return {
        "tool": "validate_team_scale",
        "generated_at": datetime.now(UTC).isoformat(),
        "fictional_data": True,
        "config": {"athletes": cfg.athletes, "requested_duration_s": cfg.duration_s, "rate_hz": cfg.rate_hz, "ticks": cfg.ticks, "request_timeout_s": cfg.timeout_s, "workers": cfg.workers, "grace_s": cfg.grace_s, "source": SOURCE},
        "backend": backend,
        "machine": machine_context(),
        "timing": {"requested_duration_s": cfg.duration_s, "actual_run_duration_s": actual_s, "run_start_utc": start_utc.isoformat()},
        "requests": count_outcomes(records),
        "latency_s": {**summarize(latencies), "measured": "round trip in this process around one POST: connection reuse, serialisation, backend validation and SQLite write, response; not backend-internal time"},
        "scheduling_lateness_s": {**summarize(lateness), "measured": "actual issue time minus due time on the monotonic clock; positive values mean the requested rate was not kept exactly"},
        "errors": [asdict(r) | {"payload": None} for r in records if r.outcome not in ("accepted", "unsent")][:200],
        "verification": verification,
        "limitations": [
            "A short run on one machine; it does not establish sustained match-length behaviour or any production capacity.",
            "Latency is measured in the client process and includes connection and serialisation time; no latency pass threshold is defined.",
            "A timed-out request may or may not have been stored; the affected athlete's count checks are reported as unknown, not as passes.",
            "All athletes, devices and values are fictional; this exercises software only, no hardware.",
        ],
    }


def print_summary(report: dict, out: Callable[[str], None] = print) -> None:
    c, r, t, lat, late, v = report["config"], report["requests"], report["timing"], report["latency_s"], report["scheduling_lateness_s"], report["verification"]["overall"]
    f = lambda x: "n/a" if x is None else f"{x * 1000:.1f} ms"  # noqa: E731
    out("TEAM-SCALE VALIDATION (fictional athletes, isolated backend)")
    out(f"Backend: {report['backend']['base_url']} pid {report['backend']['pid']} db {report['backend']['db_path']}")
    out(f"Config: {c['athletes']} athletes · {c['rate_hz']:g} Hz per stream · {c['ticks']} ticks (last due at {(c['ticks'] - 1) / c['rate_hz']:.1f} s) · requested {t['requested_duration_s']:.1f} s · actual run {t['actual_run_duration_s']:.2f} s (start to last response) · timeout {c['request_timeout_s']:g} s")
    out(f"Requests: expected {r['expected']} · attempted {r['attempted']} · accepted {r['accepted']} · failed {r['failed']} (http {r['http_error']}, timeout {r['timeout']}, connection {r['connection_error']}) · unsent {r['unsent']}")
    for s in STREAMS:
        b = r["by_stream"][s]
        out(f"  {s:14s} expected {b['expected']} · accepted {b['accepted']} · failed {b['failed']} · unsent {b['unsent']}")
    out(f"Latency (client round trip per POST): p50 {f(lat['p50'])} · p95 {f(lat['p95'])} · max {f(lat['max'])} over {lat['count']} requests")
    out(f"Scheduling lateness (issue − due): p50 {f(late['p50'])} · p95 {f(late['p95'])} · max {f(late['max'])}")
    out(f"Verification: {v['passed']} passed · {v['failed']} failed · {v['unknown']} unknown of {v['checked']} athletes · device ids distinct: {v['device_ids_distinct']} · athletes distinct: {v['athletes_distinct']}")
    for a in report["verification"]["per_athlete"]:
        if a["status"] != "passed":
            bad = [k for k, val in a["checks"].items() if val is not True]
            out(f"  {a['athlete_id']}: {a['status']} ({', '.join(bad)}) {'; '.join(a['notes'])}")
    m = report["machine"]
    out(f"Machine: {m['platform']} · Python {m['python']} · {m['cpu_count']} CPUs. Observations only: no threshold, not a capacity claim.")


# ---- command line -------------------------------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--athletes", type=int, default=DEFAULT_ATHLETES, help=f"number of fictional athletes (default {DEFAULT_ATHLETES})")
    p.add_argument("--duration", type=float, default=DEFAULT_DURATION_S, help=f"approximate run length in seconds (default {DEFAULT_DURATION_S:g})")
    p.add_argument("--rate", type=float, default=DEFAULT_RATE_HZ, help=f"samples per second per athlete per stream (default {DEFAULT_RATE_HZ:g})")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S, help=f"per-request timeout in seconds (default {DEFAULT_TIMEOUT_S:g})")
    p.add_argument("--workers", type=int, default=8, help="concurrent request workers (threads) (default 8)")
    p.add_argument("--report", type=Path, default=None, help="JSON report path (default reports/team-scale-<UTC time>.json, ignored by git)")
    p.add_argument("--port", type=int, default=None, help="port for the isolated backend (default: a free port)")
    return p


def main(argv: list[str] | None = None, *, out: Callable[[str], None] = print) -> int:
    args = build_parser().parse_args(argv)
    if args.athletes < 1 or args.duration <= 0 or args.rate <= 0 or args.timeout <= 0:
        out("athletes must be >= 1 and duration, rate and timeout must be positive")
        return 2
    cfg = RunConfig(athletes=args.athletes, duration_s=args.duration, rate_hz=args.rate, timeout_s=args.timeout, workers=args.workers)
    report_path = args.report or (ROOT / "reports" / f"team-scale-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json")
    backend = IsolatedBackend(port=args.port)
    stopping = {"done": False}

    def on_signal(signum, _frame):  # stop only the backend we started, then exit
        if not stopping["done"]:
            stopping["done"] = True
            backend.cleanup()
        raise SystemExit(130)

    previous = {s: signal.signal(s, on_signal) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        out(f"Starting an isolated backend on port {backend.port} with database {backend.db_path} (the development backend is not involved)…")
        backend.start()
        backend_info = {"base_url": backend.base_url, "pid": backend.proc.pid if backend.proc else None, "db_path": str(backend.db_path), "command": backend.cmd, "retention_cap": RETENTION_CAP}
        out(f"Sending {cfg.athletes} athletes × {cfg.ticks} ticks × 2 streams = {cfg.athletes * cfg.ticks * 2} fictional samples at {cfg.rate_hz:g} Hz…")
        records, actual_s, start_utc = run_load(backend.base_url, cfg)
        out("Reading back and verifying…")
        with httpx.Client(timeout=cfg.timeout_s) as client:
            verification = verify(client, backend.base_url, records, cfg, start_utc)
        report = build_report(cfg, backend_info, records, actual_s, start_utc, verification)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print_summary(report, out)
        out(f"JSON report: {report_path}")
        v = report["verification"]["overall"]
        return 0 if v["failed"] == 0 and report["requests"]["unsent"] == 0 else 1
    finally:
        if not stopping["done"]:
            stopping["done"] = True
            backend.cleanup()
        for s, h in previous.items():
            signal.signal(s, h)


if __name__ == "__main__":
    sys.exit(main())
