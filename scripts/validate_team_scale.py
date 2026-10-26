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

  5. Registers the fictional athletes to a fictional team before the timed run (setup is reported separately) and,
     during ingestion, reads the live team overview (GET /teams/{id}/live) at a fixed interval. Read latency is
     tracked separately from write latency, and every overview is checked: one entry per registered athlete, every
     entry's samples name that athlete (never another athlete's readings), ages never negative, and each athlete's
     latest timestamps never going backwards between reads.
  6. Optionally (--scenario interrupted) runs a small deterministic disturbance: two athletes' tracking streams are
     paused for a few ticks (samples intentionally omitted, counted apart from anything the tool failed to send), one
     athlete's physiological samples for those ticks are delivered late with their original recorded timestamps
     after newer ones, and three athletes report device statuses, including an older report re-sent later. The
     verification then checks that an older late arrival never replaces a newer latest reading, that stream recency
     stays independent, and that the latest device status is the newest report.

  7. Each stream has its own monotonic schedule and rate (--physiological-rate / --tracking-rate, or --rate for
     both). Samples are generated only on their own stream's grid: a 10 Hz tracking stream never duplicates
     physiological measurements, and every sample carries the timestamp of its own tick. Overview reads and the
     scenario's device-status reports keep their own cadence, in seconds, independent of the sample rates.

USAGE
    python scripts/validate_team_scale.py                      # 20 athletes, ~15 s, 1 Hz, overview read every 2 s
    python scripts/validate_team_scale.py --athletes 20 --duration 40 --physiological-rate 1 --tracking-rate 10
    python scripts/validate_team_scale.py --athletes 11 --duration 15
    python scripts/validate_team_scale.py --athletes 20 --duration 60                       # healthy run with reads
    python scripts/validate_team_scale.py --athletes 20 --duration 30 --scenario interrupted
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

TEAM_ID = "fictional-team"
DEFAULT_READ_INTERVAL_S = 2.0
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
    outcome: str                # accepted | http_error | timeout | connection_error | unsent | omitted
    status: int | None
    detail: str = ""
    payload: dict = field(default_factory=dict)
    plan: str = "scheduled"     # scheduled | late (delivered at a later tick, original timestamp kept) | omitted (scenario left it out)

    @property
    def lateness_s(self) -> float | None:
        return None if self.sent_s is None else self.sent_s - self.scheduled_s


@dataclass
class ReadRecord:
    """One overview read during ingestion, with its latency and the consistency checks applied to the response."""
    tick: int
    scheduled_s: float
    sent_s: float | None
    latency_s: float | None
    outcome: str                # ok | http_error | timeout | connection_error | unsent
    status: int | None
    checks: dict = field(default_factory=dict)
    detail: str = ""


@dataclass
class StatusRecord:
    """One fictional device-status report sent by the scenario."""
    athlete: str
    device_id: str
    tick: int
    payload: dict
    outcome: str
    status: int | None
    latency_s: float | None = None
    detail: str = ""


# ---- scenario plan ------------------------------------------------------------------------------------------------

@dataclass
class Plan:
    """What the scenario changes, keyed by (athlete index, tick, stream). Empty for the healthy run."""
    name: str = "healthy"
    omitted: set = field(default_factory=set)       # (i, tick, stream): intentionally not sent
    late: dict = field(default_factory=dict)        # (i, tick, stream) -> delivery tick (original timestamp kept)
    statuses: dict = field(default_factory=dict)    # delivery tick -> list of (i, status tick, battery)
    paused_tracking: list = field(default_factory=list)
    late_physiological: list = field(default_factory=list)
    status_athletes: list = field(default_factory=list)
    status_ticks: tuple = ()

    def deliveries_at(self, tick: int) -> list[tuple[int, int, str]]:
        return sorted([k for k, d in self.late.items() if d == tick], key=lambda k: (-k[1], k[0], k[2]))  # newest first, so older ones arrive after newer

    @property
    def delivery_ticks(self) -> set:
        return set(self.late.values())


PAUSE_WINDOW_S = (5.0, 10.0)      # tracking paused for samples due in [5 s, 10 s)
LATE_DELIVERY_S = 12.0            # late physiological samples (due in the pause window) are delivered at 12 s
STATUS_REPORT_S = (0.0, 10.0)     # device statuses reported at these seconds; the first one is re-sent at LATE_DELIVERY_S


def stream_ticks(cfg: "RunConfig", stream: str) -> int:
    return max(1, int(round(cfg.duration_s * cfg.rate_for(stream))))


def ticks_in(cfg: "RunConfig", stream: str, lo_s: float, hi_s: float) -> list[int]:
    """Ticks of a stream whose due time lies in [lo_s, hi_s)."""
    rate = cfg.rate_for(stream)
    return [t for t in range(stream_ticks(cfg, stream)) if lo_s <= t / rate < hi_s]


def tick_at(cfg: "RunConfig", stream: str, seconds: float) -> int:
    return int(round(seconds * cfg.rate_for(stream)))


def scenario_plan(name: str, athletes: int, cfg: "RunConfig") -> Plan:
    """A small, reproducible disturbance defined in seconds and mapped onto each stream's own grid; identical for the same arguments.

    Needs at least 3 athletes and a run of at least 13 s (so the late delivery at 12 s and the status re-send happen).
    """
    if name == "healthy":
        return Plan()
    if name != "interrupted":
        raise ValueError(f"unknown scenario {name!r}")
    if athletes < 3 or cfg.duration_s < 13:
        raise ValueError("the interrupted scenario needs at least 3 athletes and a duration of at least 13 s")
    plan = Plan(name="interrupted")
    plan.paused_tracking = [0, 1]
    plan.late_physiological = [2]
    plan.status_athletes = [0, 1, 2]
    plan.status_ticks = STATUS_REPORT_S
    for i in plan.paused_tracking:
        for t in ticks_in(cfg, "tracking", *PAUSE_WINDOW_S):
            plan.omitted.add((i, t, "tracking"))
    late_tick = tick_at(cfg, "physiological", LATE_DELIVERY_S)
    for i in plan.late_physiological:
        for t in ticks_in(cfg, "physiological", *PAUSE_WINDOW_S):
            plan.late[(i, t, "physiological")] = late_tick
    plan.statuses = {STATUS_REPORT_S[0]: [(i, STATUS_REPORT_S[0], 90.0) for i in plan.status_athletes], STATUS_REPORT_S[1]: [(i, STATUS_REPORT_S[1], 80.0) for i in plan.status_athletes], LATE_DELIVERY_S: [(i, STATUS_REPORT_S[0], 90.0) for i in plan.status_athletes]}  # keyed by delivery second
    return plan


def status_payload(i: int, status_s: float, battery: float, start_utc: datetime) -> dict:
    return {"device_id": device_id(i, "physiological"), "timestamp": (start_utc + timedelta(seconds=status_s)).isoformat(), "connected": True, "battery_percent": battery, "signal_quality": 0.9}


# ---- overview reads ------------------------------------------------------------------------------------------------

def check_pause_in_progress(body: dict, plan: "Plan", at_s: float, start_utc: datetime, physiological_rate_hz: float) -> bool | None:
    """While the tracking pause is in progress, a paused athlete's tracking reading must be from before the pause while the
    physiological reading is current (at most one physiological tick old). None when the read is outside the decidable window."""
    if not plan.paused_tracking:
        return None
    lo, hi = PAUSE_WINDOW_S
    if not (lo < at_s < hi):          # at hi the first post-pause tracking tick is due, so the pause is no longer decidable
        return None
    rows = {r["athlete"]["id"]: r for r in body.get("athletes", [])}
    stale_before = start_utc + timedelta(seconds=lo)                              # tracking must be older than the pause start
    current_after = start_utc + timedelta(seconds=at_s - 1.0 / physiological_rate_hz)  # physiological must be at least this recent
    for i in plan.paused_tracking:
        row = rows.get(athlete_id(i))
        if row is None or row["tracking"] is None or row["physiological"] is None:
            return False
        trk = datetime.fromisoformat(row["tracking"]["timestamp"].replace("Z", "+00:00"))
        phys = datetime.fromisoformat(row["physiological"]["timestamp"].replace("Z", "+00:00"))
        if not (trk < stale_before and phys >= current_after):
            return False
    return True


def check_overview(body: dict, expected_ids: list[str], previous_latest: dict) -> dict:
    """Consistency checks on one team overview; updates previous_latest (athlete -> {stream: timestamp}) in place."""
    rows = body.get("athletes", [])
    ids = [r["athlete"]["id"] for r in rows]
    checks = {
        "one_entry_per_registered_athlete": sorted(ids) == sorted(expected_ids),
        "samples_name_their_own_athlete": all((r["physiological"] is None or r["physiological"]["athlete_id"] == r["athlete"]["id"]) and (r["tracking"] is None or r["tracking"]["athlete_id"] == r["athlete"]["id"]) for r in rows),
        "ages_never_negative": all((r["physiological_age_seconds"] is None or r["physiological_age_seconds"] >= 0) and (r["tracking_age_seconds"] is None or r["tracking_age_seconds"] >= 0) for r in rows),
        "latest_timestamps_never_go_backwards": True,
        "device_ids_match_samples": all(all(d["device_id"] in {x["device_id"] for x in (r["physiological"], r["tracking"]) if x and x.get("device_id")} for d in r.get("devices", [])) for r in rows),
    }
    for r in rows:
        prev = previous_latest.setdefault(r["athlete"]["id"], {})
        for stream in STREAMS:
            sample = r[stream]
            if sample is None:
                continue
            ts = datetime.fromisoformat(sample["timestamp"].replace("Z", "+00:00"))
            if stream in prev and ts < prev[stream]:
                checks["latest_timestamps_never_go_backwards"] = False
            prev[stream] = max(ts, prev.get(stream, ts))
    return checks


def send_one(client: httpx.Client, base_url: str, stream: str, payload: dict, clock: Callable[[], float], path: str | None = None) -> tuple[float, str, int | None, str]:
    """POST one sample (or, with `path`, any payload); returns (latency, outcome, status, detail). Never retries."""
    t0 = clock()
    try:
        r = client.post(f"{base_url}{path or '/api/v1/samples/' + stream}", json=payload)
    except httpx.TimeoutException as exc:
        return clock() - t0, "timeout", None, f"{type(exc).__name__}: acceptance unknown"
    except httpx.HTTPError as exc:
        return clock() - t0, "connection_error", None, type(exc).__name__
    latency = clock() - t0
    if r.status_code == 200:
        return latency, "accepted", 200, ""
    return latency, "http_error", r.status_code, r.text[:200]


def read_overview(client: httpx.Client, base_url: str, team_id: str, clock: Callable[[], float]) -> tuple[float, str, int | None, dict | None, str]:
    """GET the team overview once; returns (latency, outcome, status, body or None, detail). Never retries."""
    t0 = clock()
    try:
        r = client.get(f"{base_url}/api/v1/teams/{team_id}/live")
    except httpx.TimeoutException as exc:
        return clock() - t0, "timeout", None, None, type(exc).__name__
    except httpx.HTTPError as exc:
        return clock() - t0, "connection_error", None, None, type(exc).__name__
    latency = clock() - t0
    if r.status_code != 200:
        return latency, "http_error", r.status_code, None, r.text[:200]
    try:
        return latency, "ok", 200, r.json(), ""
    except ValueError:
        return latency, "http_error", 200, None, "response was not JSON"


# ---- the run ------------------------------------------------------------------------------------------------------

@dataclass
class RunConfig:
    athletes: int = DEFAULT_ATHLETES
    duration_s: float = DEFAULT_DURATION_S
    rate_hz: float = DEFAULT_RATE_HZ
    timeout_s: float = DEFAULT_TIMEOUT_S
    workers: int = 8
    grace_s: float = 10.0       # after the last tick, how long to wait for outstanding requests before marking the rest unsent
    read_interval_s: float = DEFAULT_READ_INTERVAL_S   # overview read every this many seconds of due time; 0 disables reads
    scenario: str = "healthy"
    team_id: str = TEAM_ID
    physiological_rate_hz: float | None = None   # None: use rate_hz
    tracking_rate_hz: float | None = None        # None: use rate_hz

    def rate_for(self, stream: str) -> float:
        """The stream's own rate: the explicit per-stream option when given, otherwise --rate."""
        v = self.physiological_rate_hz if stream == "physiological" else self.tracking_rate_hz
        return self.rate_hz if v is None else v

    @property
    def ticks(self) -> int:
        """Physiological ticks (kept for callers that think in one grid; use stream_ticks for the tracking grid)."""
        return stream_ticks(self, "physiological")

    @property
    def expected_samples(self) -> int:
        return self.athletes * sum(stream_ticks(self, s) for s in STREAMS)


@dataclass
class RunResult:
    records: list            # RequestRecord per (athlete, tick, stream), omitted ones included
    reads: list              # ReadRecord per overview read
    statuses: list           # StatusRecord per device-status report
    actual_s: float
    start_utc: datetime
    plan: Plan


def run_load(
    base_url: str,
    cfg: RunConfig,
    *,
    make_client: Callable[[], httpx.Client] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    start_utc: datetime | None = None,
    plan: Plan | None = None,
) -> RunResult:
    """Send every scheduled sample (and the scenario's late deliveries, statuses and overview reads) on monotonic schedules.

    Each stream has its own grid: tick t of a stream is due at start + t / rate_for(stream) and carries that instant as
    its timestamp. Overview reads are due every read_interval_s and statuses at their scenario seconds, independently of
    the sample rates. Work is handed to a small thread pool when due (never earlier); lateness is how long after its due
    time a request was actually issued. Work that could not be issued before the deadline (last due time + grace) is
    recorded as unsent, never as a success. Samples the scenario leaves out are recorded as omitted, apart from unsent
    ones. Late deliveries keep the timestamp of the tick they belong to; only their due time moves.
    """
    start_utc = start_utc or datetime.now(UTC)
    plan = plan or Plan()
    records: list[RequestRecord] = []
    reads: list[ReadRecord] = []
    statuses: list[StatusRecord] = []
    lock = threading.Lock()
    make_client = make_client or (lambda: httpx.Client(timeout=cfg.timeout_s))
    client = make_client()
    expected_ids = [athlete_id(i) for i in range(cfg.athletes)]
    previous_latest: dict = {}
    rates = {stream: cfg.rate_for(stream) for stream in STREAMS}

    # ---- the schedule: (due_s, order, kind, payload) sorted by due time; samples first at equal instants ----
    events: list[tuple[float, int, str, tuple]] = []
    for stream in STREAMS:
        for tick in range(stream_ticks(cfg, stream)):
            due = tick / rates[stream]
            for i in range(cfg.athletes):
                key = (i, tick, stream)
                if key in plan.omitted:
                    events.append((due, 0, "omitted", (i, tick, stream)))
                elif key in plan.late:
                    continue   # delivered at its delivery tick below
                else:
                    events.append((due, 0, "sample", (i, tick, stream, "scheduled")))
    for (i, tick, stream), delivery_tick in plan.late.items():
        events.append((delivery_tick / rates[stream], 1, "sample", (i, tick, stream, "late")))
    for second, items in plan.statuses.items():
        for (i, status_s, battery) in items:
            events.append((float(second), 2, "status", (i, status_s, battery)))
    if cfg.read_interval_s > 0:
        n_reads = int(cfg.duration_s // cfg.read_interval_s) + 1
        for k in range(n_reads):
            due = k * cfg.read_interval_s
            if due < cfg.duration_s:
                events.append((due, 3, "read", (k,)))
    events.sort(key=lambda e: (e[0], e[1]))
    last_due = max((e[0] for e in events), default=0.0)
    start = clock()
    deadline = start + last_due + cfg.grace_s

    def sample_job(i: int, tick: int, stream: str, due: float, kind: str) -> None:
        stamp = start_utc + timedelta(seconds=tick / rates[stream])   # the sample's own tick, also for late deliveries
        payload = sample_payloads(i, tick, cfg.duration_s, rates[stream], stamp)[stream]
        if clock() > deadline:
            rec = RequestRecord(athlete_id(i), stream, tick, due, None, None, "unsent", None, "deadline passed before this request was issued", payload, kind)
        else:
            sent = clock()
            latency, outcome, status, detail = send_one(client, base_url, stream, payload, clock)
            rec = RequestRecord(athlete_id(i), stream, tick, due, sent - start, latency, outcome, status, detail, payload, kind)
        with lock:
            records.append(rec)

    def status_job(i: int, status_s: float, battery: float, due: float) -> None:
        payload = status_payload(i, status_s, battery, start_utc)
        if clock() > deadline:
            rec = StatusRecord(athlete_id(i), payload["device_id"], int(due), payload, "unsent", None, None, "deadline passed")
        else:
            latency, outcome, status, detail = send_one(client, base_url, "", payload, clock, path="/api/v1/devices/status")
            rec = StatusRecord(athlete_id(i), payload["device_id"], int(due), payload, "accepted" if outcome == "accepted" else outcome, status, latency, detail)
        with lock:
            statuses.append(rec)

    def read_job(k: int, due: float) -> None:
        if clock() > deadline:
            rec = ReadRecord(k, due, None, None, "unsent", None, {}, "deadline passed")
        else:
            sent = clock()
            latency, outcome, status, body, detail = read_overview(client, base_url, cfg.team_id, clock)
            checks = {}
            if body is not None:
                with lock:
                    checks = check_overview(body, expected_ids, previous_latest)
                in_pause = check_pause_in_progress(body, plan, due, start_utc, rates["physiological"])
                if in_pause is not None:
                    checks["paused_tracking_stale_while_physiological_current"] = in_pause
            rec = ReadRecord(k, due, sent - start, latency, outcome, status, checks, detail)
        with lock:
            reads.append(rec)

    try:
        with ThreadPoolExecutor(max_workers=max(1, cfg.workers)) as pool:
            futures = []
            for due, _order, kind, args in events:
                wait = start + due - clock()
                if wait > 0:
                    sleep(wait)
                if kind == "omitted":
                    i, tick, stream = args
                    with lock:
                        stamp = (start_utc + timedelta(seconds=tick / rates[stream])).isoformat()
                        records.append(RequestRecord(athlete_id(i), stream, tick, due, None, None, "omitted", None, "left out by the scenario on purpose", {"timestamp": stamp}, "omitted"))
                elif kind == "sample":
                    i, tick, stream, plan_kind = args
                    futures.append(pool.submit(sample_job, i, tick, stream, due, plan_kind))
                elif kind == "status":
                    i, status_s, battery = args
                    futures.append(pool.submit(status_job, i, status_s, battery, due))
                else:
                    futures.append(pool.submit(read_job, args[0], due))
            for f in futures:
                remaining = deadline - clock()
                try:
                    f.result(timeout=max(0.0, remaining))
                except Exception:  # noqa: BLE001  a worker that did not finish in time: its request is recorded by the job as unsent or not at all
                    pass
    finally:
        client.close()
    actual = clock() - start
    seen = {(r.athlete, r.tick, r.stream) for r in records}
    for stream in STREAMS:
        for tick in range(stream_ticks(cfg, stream)):
            for i in range(cfg.athletes):
                if (athlete_id(i), tick, stream) not in seen:
                    records.append(RequestRecord(athlete_id(i), stream, tick, tick / rates[stream], None, None, "unsent", None, "never issued", {}, "late" if (i, tick, stream) in plan.late else "scheduled"))
    records.sort(key=lambda r: (r.scheduled_s, r.athlete, r.stream, r.tick))
    reads.sort(key=lambda r: r.tick)
    return RunResult(records, reads, statuses, actual, start_utc, plan)


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


def count_reads(reads: list[ReadRecord]) -> dict:
    out = {"expected": len(reads), "ok": 0, "failed": 0, "unsent": 0, "http_error": 0, "timeout": 0, "connection_error": 0, "checks_failed": {}, "checks_evaluated": {}}
    for r in reads:
        if r.outcome == "unsent":
            out["unsent"] += 1
        elif r.outcome == "ok":
            out["ok"] += 1
            for k, v in r.checks.items():
                out["checks_evaluated"][k] = out["checks_evaluated"].get(k, 0) + 1
                if v is False:
                    out["checks_failed"][k] = out["checks_failed"].get(k, 0) + 1
        else:
            out["failed"] += 1; out[r.outcome] += 1
    return out


def stream_report(records: list[RequestRecord], stream: str, requested_rate_hz: float, athletes: int) -> dict:
    """Per-stream accounting, delivered rate per athlete over the stream's own schedule span, lateness and latency."""
    mine = [r for r in records if r.stream == stream]
    sent = [r for r in mine if r.sent_s is not None]
    accepted = [r for r in mine if r.outcome == "accepted"]
    dues = [r.scheduled_s for r in mine if r.plan != "late"]
    interval = (max(dues) - min(dues) + 1.0 / requested_rate_hz) if dues else 0.0   # the schedule span: first due to one period after the last due
    return {
        "requested_rate_hz": requested_rate_hz,
        "expected": len(mine), "attempted": len(sent), "accepted": len(accepted),
        "failed": sum(1 for r in mine if r.outcome in ("http_error", "connection_error")),
        "unknown_timeout": sum(1 for r in mine if r.outcome == "timeout"),
        "omitted_by_scenario": sum(1 for r in mine if r.outcome == "omitted"),
        "unsent": sum(1 for r in mine if r.outcome == "unsent"),
        "delivered_late": sum(1 for r in mine if r.plan == "late" and r.outcome == "accepted"),
        "measurement_interval_s": interval,
        "delivered_rate_hz": (len(accepted) / interval / max(1, athletes)) if interval > 0 else None,
        "delivered_rate_note": "accepted samples per athlete divided by the stream's schedule span (first due time to one period after the last due); omitted, failed and unsent samples lower it",
        "latency_s": summarize([r.latency_s for r in sent if r.latency_s is not None]),
        "scheduling_lateness_s": summarize([r.lateness_s for r in sent if r.lateness_s is not None]),
    }


def count_outcomes(records: list[RequestRecord]) -> dict:
    out: dict = {"expected": len(records), "attempted": 0, "accepted": 0, "failed": 0, "unsent": 0, "omitted_by_scenario": 0, "delivered_late": 0, "http_error": 0, "timeout": 0, "connection_error": 0, "by_stream": {}, "http_status_counts": {}}
    for s in STREAMS:
        out["by_stream"][s] = {"expected": 0, "attempted": 0, "accepted": 0, "failed": 0, "unsent": 0, "omitted_by_scenario": 0, "timeout": 0}
    for r in records:
        b = out["by_stream"][r.stream]
        b["expected"] += 1
        if r.outcome == "omitted":
            out["omitted_by_scenario"] += 1; b["omitted_by_scenario"] += 1
            continue
        if r.outcome == "unsent":
            out["unsent"] += 1; b["unsent"] += 1
            continue
        if r.plan == "late":
            out["delivered_late"] += 1
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
        spans: dict = {}
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
            spans = {}
            for s in STREAMS:
                lo, hi = retained[s]["earliest_timestamp"], retained[s]["latest_timestamp"]
                spans[s] = {"retained": retained[s]["sample_count"], "span_s": (datetime.fromisoformat(hi.replace("Z", "+00:00")) - datetime.fromisoformat(lo.replace("Z", "+00:00"))).total_seconds() if lo and hi else None,
                            "capped": retained[s]["sample_count"] >= RETENTION_CAP}
            notes.append("retained: " + "; ".join(f"{s} {v['retained']} rows spanning {v['span_s']} s" + (" (at the 300-row cap)" if v["capped"] else "") for s, v in spans.items()))
            if exp["physiological"] or exp["tracking"]:
                last_ts = max((r.payload["timestamp"] for s in STREAMS for r in exp[s]), key=lambda t: datetime.fromisoformat(t))
                window_s = min(300, max(1, int(cfg.duration_s) + 1))
                end_dt = datetime.fromisoformat(last_ts)
                in_window = {s: [r for r in exp[s] if datetime.fromisoformat(r.payload["timestamp"]) >= end_dt - timedelta(seconds=window_s)] for s in STREAMS}  # each stream on its own grid, after its own cap
                win = client.get(f"{base_url}/api/v1/athletes/{a}/window", params={"seconds": window_s, "end": last_ts}).json()
                checks["window_has_both_streams"] = len(win["physiological_samples"]) == len(in_window["physiological"]) and len(win["tracking_samples"]) == len(in_window["tracking"])
                summary = client.get(f"{base_url}/api/v1/athletes/{a}/summary", params={"seconds": window_s, "end": last_ts}).json()
                p, t = summary["physiological"], summary["tracking"]
                hrs = [r.payload["heart_rate_bpm"] for r in in_window["physiological"]]
                speeds = [r.payload["speed_mps"] for r in in_window["tracking"]]
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
        per_athlete.append({"athlete_id": a, "status": status, "checks": checks, "notes": notes, "retained": spans})
    # athletes and devices separate: every athlete's latest samples name its own ids (checked above) and no device id is shared
    all_devices = [d for ds in seen_devices.values() for d in ds]
    overall["device_ids_distinct"] = len(all_devices) == len(set(all_devices))
    overall["athletes_distinct"] = len({p["athlete_id"] for p in per_athlete}) == cfg.athletes
    return {"overall": overall, "per_athlete": per_athlete}


def verify_overview(client: httpx.Client, base_url: str, records: list[RequestRecord], statuses: list[StatusRecord], cfg: RunConfig, plan: Plan) -> dict:
    """The final team overview against the accepted samples and the scenario: ordering, separation, recency, devices."""
    expected_ids = [athlete_id(i) for i in range(cfg.athletes)]
    checks: dict = {}
    notes: list[str] = []
    try:
        body = client.get(f"{base_url}/api/v1/teams/{cfg.team_id}/live").json()
    except Exception as exc:  # noqa: BLE001
        return {"checks": {"read": False}, "notes": [f"overview read failed: {type(exc).__name__}: {exc}"]}
    rows = {r["athlete"]["id"]: r for r in body.get("athletes", [])}
    checks.update(check_overview(body, expected_ids, {}))
    timed_out = {r.athlete for r in records if r.outcome == "timeout"}   # acceptance unknown for these athletes
    unknown_notes: list[str] = []

    def settle(ok: bool, uncertain: bool) -> bool | None:
        """A check that found no mismatch but depended on an unknown outcome is unknown (None), never a pass."""
        return False if not ok else (None if uncertain else True)

    # newest-timestamp ordering: the overview's latest per stream is the newest ACCEPTED timestamp, whatever order things arrived in
    ordering_ok, ordering_uncertain = True, False
    for i in range(cfg.athletes):
        a = athlete_id(i)
        row = rows.get(a)
        if row is None:
            ordering_ok = False
            continue
        for s in STREAMS:
            exp = expected_retained(records, a, s)
            got = row[s]
            match = (got is None) if not exp else (got is not None and datetime.fromisoformat(got["timestamp"].replace("Z", "+00:00")) == datetime.fromisoformat(exp[-1].payload["timestamp"]))
            if not match:
                if a in timed_out:
                    ordering_uncertain = True   # a timed-out sample may have been stored: the mismatch is not a proven defect
                else:
                    ordering_ok = False
    checks["latest_is_newest_accepted_timestamp"] = settle(ordering_ok, ordering_uncertain)
    if ordering_uncertain:
        unknown_notes.append("latest-timestamp ordering could not be settled for athletes with timed-out requests")
    if plan.late:
        late_ok, late_uncertain = True, False
        for (i, tick, s), _ in plan.late.items():
            a = athlete_id(i); row = rows.get(a)
            if a in timed_out:
                late_uncertain = True
            if row is None or row[s] is None:
                late_ok = False; continue
            latest_tick_ts = datetime.fromisoformat(row[s]["timestamp"].replace("Z", "+00:00"))
            accepted = [r for r in records if r.athlete == a and r.stream == s and r.outcome == "accepted"]
            newest_on_time = max((datetime.fromisoformat(r.payload["timestamp"]) for r in accepted if r.plan == "scheduled"), default=None)
            if newest_on_time is not None and latest_tick_ts < newest_on_time:
                late_ok = False
        checks["older_late_arrival_did_not_replace_newer_latest"] = settle(late_ok, late_uncertain)
        notes.append(f"{len(plan.late)} physiological samples were delivered late at {LATE_DELIVERY_S:g} s with their original timestamps")
    if plan.omitted:
        # independent stream recency, settled from the retained history after the run: for paused athletes no tracking sample
        # exists for the paused ticks while their physiological samples for those ticks do. The in-progress view is checked
        # on the overview reads taken during the pause (paused_tracking_stale_while_physiological_current).
        pause_ok, pause_uncertain = True, False
        for i in plan.paused_tracking:
            a = athlete_id(i)
            if a in timed_out:
                pause_uncertain = True
            try:
                hist = client.get(f"{base_url}/api/v1/athletes/{a}/tracking/history?limit={RETENTION_CAP}").json()
                phys_hist = client.get(f"{base_url}/api/v1/athletes/{a}/physiological/history?limit={RETENTION_CAP}").json()
            except Exception:  # noqa: BLE001
                pause_ok = False; continue
            paused_ticks = sorted({t for (j, t, s) in plan.omitted if j == i and s == "tracking"})
            iso = lambda t: datetime.fromisoformat(str(t).replace("Z", "+00:00"))  # noqa: E731  compare instants, not text
            omitted_stamps = {iso(r.payload["timestamp"]) for r in records if r.athlete == a and r.stream == "tracking" and r.outcome == "omitted" and r.payload.get("timestamp")}
            track_stamps = {iso(h["timestamp"]) for h in hist}
            phys_stamps = {iso(h["timestamp"]) for h in phys_hist}
            exp_phys = {iso(r.payload["timestamp"]) for r in records if r.athlete == a and r.stream == "physiological" and r.outcome == "accepted" and r.tick in paused_ticks}
            if omitted_stamps & track_stamps:
                pause_ok = False            # an omitted sample can never have been stored: a definite defect
            elif not exp_phys.issubset(phys_stamps) and a not in timed_out:
                pause_ok = False
        checks["paused_stream_absent_while_other_stream_continued"] = settle(pause_ok, pause_uncertain)
        notes.append(f"{len(plan.omitted)} tracking samples were left out on purpose for athletes {[athlete_id(i) for i in plan.paused_tracking]} (due between {PAUSE_WINDOW_S[0]:g} s and {PAUSE_WINDOW_S[1]:g} s)")
    if plan.statuses:
        dev_ok, dev_uncertain, dev_checked = True, False, 0
        for i in plan.status_athletes:
            a = athlete_id(i); row = rows.get(a)
            mine = [x for x in statuses if x.athlete == a]
            if any(x.outcome == "timeout" for x in mine):
                dev_uncertain = True        # a timed-out report may have been stored
            sent_ok = [x for x in mine if x.outcome == "accepted"]
            if not sent_ok:
                continue   # nothing accepted: nothing to verify (reported in counts)
            dev_checked += 1
            newest = max(sent_ok, key=lambda x: x.payload["timestamp"])
            devs = {d["device_id"]: d for d in (row.get("devices", []) if row else [])}
            d = devs.get(device_id(i, "physiological"))
            if d is None or d["status"] is None or datetime.fromisoformat(d["status"]["timestamp"].replace("Z", "+00:00")) != datetime.fromisoformat(newest.payload["timestamp"]) or d["status"]["battery_percent"] != newest.payload["battery_percent"]:
                if not dev_uncertain:
                    dev_ok = False
        checks["device_status_is_newest_report_old_resend_ignored"] = settle(dev_ok, dev_uncertain) if dev_checked else None
        notes.append(f"device statuses were sent at {STATUS_REPORT_S[0]:g} s and {STATUS_REPORT_S[1]:g} s and the first report was re-sent at {LATE_DELIVERY_S:g} s")
    if unknown_notes:
        notes.extend(unknown_notes)
    return {"checks": checks, "notes": notes}


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


def build_report(cfg: RunConfig, backend: dict, records: list[RequestRecord], actual_s: float, start_utc: datetime, verification: dict,
                 reads: list | None = None, statuses: list | None = None, setup: dict | None = None, overview_verification: dict | None = None, plan: Plan | None = None) -> dict:
    reads = reads or []; statuses = statuses or []; plan = plan or Plan()
    sent = [r for r in records if r.sent_s is not None]
    latencies = [r.latency_s for r in sent if r.latency_s is not None]
    lateness = [r.lateness_s for r in sent if r.lateness_s is not None]
    read_sent = [r for r in reads if r.sent_s is not None]
    status_sent = [x for x in statuses if x.latency_s is not None]
    return {
        "setup": setup or {},
        "scenario": {"name": plan.name, "paused_tracking_athletes": [athlete_id(i) for i in plan.paused_tracking], "late_physiological_athletes": [athlete_id(i) for i in plan.late_physiological],
                     "omitted_samples": len(plan.omitted), "late_deliveries": len(plan.late), "status_reports_planned": sum(len(v) for v in plan.statuses.values())},
        "reads": {**count_reads(reads), "interval_s": cfg.read_interval_s, "team_id": cfg.team_id},
        "read_latency_s": {**summarize([r.latency_s for r in read_sent if r.latency_s is not None]), "measured": "round trip in this process around one GET /teams/{id}/live during ingestion"},
        "read_lateness_s": summarize([r.sent_s - r.scheduled_s for r in read_sent]),
        "device_status_reports": {"sent": len(status_sent), "accepted": sum(1 for x in statuses if x.outcome == "accepted"), "failed": sum(1 for x in statuses if x.outcome not in ("accepted", "unsent")), "unsent": sum(1 for x in statuses if x.outcome == "unsent")},
        "overview_verification": overview_verification or {},
        "tool": "validate_team_scale",
        "generated_at": datetime.now(UTC).isoformat(),
        "fictional_data": True,
        "config": {"athletes": cfg.athletes, "requested_duration_s": cfg.duration_s, "rate_hz": cfg.rate_hz, "physiological_rate_hz": cfg.rate_for("physiological"), "tracking_rate_hz": cfg.rate_for("tracking"),
                   "ticks": {s: stream_ticks(cfg, s) for s in STREAMS}, "request_timeout_s": cfg.timeout_s, "workers": cfg.workers, "grace_s": cfg.grace_s, "source": SOURCE,
                   "retention_cap_note": "the newest-300 cap is a row count per stream, not a duration: at 10 Hz it spans 30 s, at 1 Hz 300 s"},
        "streams": {s: stream_report(records, s, cfg.rate_for(s), cfg.athletes) for s in STREAMS},
        "retained_spans": {s: {"rows": [a["retained"][s]["retained"] for a in verification.get("per_athlete", []) if a.get("retained")], "span_s": [a["retained"][s]["span_s"] for a in verification.get("per_athlete", []) if a.get("retained")]} for s in STREAMS},
        "backend": backend,
        "machine": machine_context(),
        "timing": {"requested_duration_s": cfg.duration_s, "actual_run_duration_s": actual_s, "run_start_utc": start_utc.isoformat()},
        "requests": count_outcomes(records),
        "latency_s": {**summarize(latencies), "measured": "WRITE latency: round trip in this process around one sample POST: connection reuse, serialisation, backend validation and SQLite write, response; not backend-internal time"},
        "scheduling_lateness_s": {**summarize(lateness), "measured": "actual issue time minus due time on the monotonic clock; positive values mean the requested rate was not kept exactly"},
        "errors": [asdict(r) | {"payload": None} for r in records if r.outcome not in ("accepted", "unsent")][:200],
        "verification": verification,
        "limitations": [
            "A short run on one machine; it does not establish sustained match-length behaviour or any production capacity.",
            "Latency is measured in the client process and includes connection and serialisation time; no latency pass threshold is defined.",
            "A timed-out request may or may not have been stored; the affected athlete's count checks are reported as unknown, not as passes.",
            "Overview reads during ingestion are a few GETs per run from one client; they do not model many dashboards.",
            "The interrupted scenario is a fixed, small disturbance (two paused tracking streams, one late physiological stream, three device statuses), not a fault-injection framework.",
            "All athletes, devices and values are fictional; this exercises software only, no hardware.",
        ],
    }


def print_summary(report: dict, out: Callable[[str], None] = print) -> None:
    c, r, t, lat, late, v = report["config"], report["requests"], report["timing"], report["latency_s"], report["scheduling_lateness_s"], report["verification"]["overall"]
    f = lambda x: "n/a" if x is None else f"{x * 1000:.1f} ms"  # noqa: E731
    out("TEAM-SCALE VALIDATION (fictional athletes, isolated backend)")
    out(f"Backend: {report['backend']['base_url']} pid {report['backend']['pid']} db {report['backend']['db_path']}")
    out(f"Config: {c['athletes']} athletes · physiological {c['physiological_rate_hz']:g} Hz ({c['ticks']['physiological']} ticks) · tracking {c['tracking_rate_hz']:g} Hz ({c['ticks']['tracking']} ticks) · requested {t['requested_duration_s']:.1f} s · actual run {t['actual_run_duration_s']:.2f} s (start to last response) · timeout {c['request_timeout_s']:g} s")
    out(f"Sample requests: expected {r['expected']} · attempted {r['attempted']} · accepted {r['accepted']} · failed {r['failed']} (http {r['http_error']}, timeout {r['timeout']}, connection {r['connection_error']}) · unsent {r['unsent']} · omitted by scenario {r['omitted_by_scenario']}")
    for s in STREAMS:
        b = report["streams"][s]
        rs = report.get("retained_spans", {}).get(s, {})
        spans = [x for x in rs.get("span_s", []) if x is not None]
        out(f"  {s:14s} requested {b['requested_rate_hz']:g} Hz · expected {b['expected']} · attempted {b['attempted']} · accepted {b['accepted']} · failed {b['failed']} · unknown (timeout) {b['unknown_timeout']} · omitted by scenario {b['omitted_by_scenario']} · unsent {b['unsent']}")
        out(f"  {'':14s} delivered {b['delivered_rate_hz'] if b['delivered_rate_hz'] is None else round(b['delivered_rate_hz'], 3)} Hz over a {b['measurement_interval_s']:.1f} s schedule span · latency p50 {f(b['latency_s']['p50'])} p95 {f(b['latency_s']['p95'])} max {f(b['latency_s']['max'])} · lateness p50 {f(b['scheduling_lateness_s']['p50'])} p95 {f(b['scheduling_lateness_s']['p95'])} max {f(b['scheduling_lateness_s']['max'])}")
        if rs.get("rows"):
            out(f"  {'':14s} retained per athlete: {min(rs['rows'])}-{max(rs['rows'])} rows spanning {min(spans):.1f}-{max(spans):.1f} s" + (" (at the 300-row cap: a count, not a duration)" if max(rs['rows']) >= RETENTION_CAP else ""))
    out(f"Write latency (client round trip per sample POST): p50 {f(lat['p50'])} · p95 {f(lat['p95'])} · max {f(lat['max'])} over {lat['count']} requests")
    out(f"Scheduling lateness (issue − due): p50 {f(late['p50'])} · p95 {f(late['p95'])} · max {f(late['max'])}")
    rd, rl = report.get("reads", {}), report.get("read_latency_s", {})
    if rd.get("expected"):
        out(f"Overview reads (GET /teams/{rd['team_id']}/live every {rd['interval_s']:g} s): expected {rd['expected']} · ok {rd['ok']} · failed {rd['failed']} (timeout {rd['timeout']}) · unsent {rd['unsent']} · checks failed {rd['checks_failed'] or 'none'}")
        out(f"Read latency: p50 {f(rl.get('p50'))} · p95 {f(rl.get('p95'))} · max {f(rl.get('max'))} over {rl.get('count')} reads")
    sc, ds = report.get("scenario", {}), report.get("device_status_reports", {})
    if sc.get("name") and sc["name"] != "healthy":
        out(f"Scenario {sc['name']}: {sc['omitted_samples']} samples left out on purpose ({r['omitted_by_scenario']} counted as omitted, apart from {r['unsent']} unsent and {r['timeout']} unknown) · {sc['late_deliveries']} delivered late with original timestamps ({r['delivered_late']} delivered) · device status reports {ds.get('accepted')}/{ds.get('sent')} accepted")
    ov = report.get("overview_verification", {})
    if ov.get("checks"):
        failed = [k for k, v in ov["checks"].items() if v is False]
        unknown = [k for k, v in ov["checks"].items() if v is None]
        verdict = "all checks passed" if not failed and not unknown else ((("FAILED " + ", ".join(failed)) if failed else "") + ((" · UNKNOWN " if failed else "UNKNOWN (outcome depends on timed-out requests) ") + ", ".join(unknown) if unknown else ""))
        out("Overview verification: " + verdict + " · " + "; ".join(ov.get("notes", [])))
    if rd.get("expected"):
        out(f"Overview checks evaluated per read: {rd.get('checks_evaluated') or 'none'}")
    st = report.get("setup", {})
    if st:
        out(f"Setup (before the timed run, not counted above): {st.get('registered')} athletes registered to team {st.get('team_id')} in {st.get('seconds', 0):.2f} s")
    out(f"Verification: {v['passed']} passed · {v['failed']} failed · {v['unknown']} unknown of {v['checked']} athletes · device ids distinct: {v['device_ids_distinct']} · athletes distinct: {v['athletes_distinct']}")
    for a in report["verification"]["per_athlete"]:
        if a["status"] != "passed":
            bad = [k for k, val in a["checks"].items() if val is not True]
            out(f"  {a['athlete_id']}: {a['status']} ({', '.join(bad)}) {'; '.join(a['notes'])}")
    m = report["machine"]
    out(f"Machine: {m['platform']} · Python {m['python']} · {m['cpu_count']} CPUs. Observations only: no threshold, not a capacity claim.")


# ---- command line -------------------------------------------------------------------------------------------------------

def register_team(base_url: str, cfg: RunConfig, out: Callable[[str], None] = print, client: httpx.Client | None = None, clock: Callable[[], float] = time.monotonic) -> dict:
    """Register the fictional athletes to the fictional team before the timed run. Reported apart from the measured traffic."""
    own = client is None
    client = client or httpx.Client(timeout=cfg.timeout_s)
    t0 = clock(); registered = 0; existing = 0; failed = 0
    try:
        for i in range(cfg.athletes):
            body = {"id": athlete_id(i), "name": f"Fictional Athlete {i + 1:02d}", "jersey_number": i + 1, "sport": "fictional", "team_id": cfg.team_id}
            r = client.post(f"{base_url}/api/v1/athletes", json=body)
            if r.status_code == 201:
                registered += 1
            elif r.status_code == 409:
                existing += 1
            else:
                failed += 1
    finally:
        if own:
            client.close()
    seconds = clock() - t0
    out(f"Setup: registered {registered} fictional athletes to team {cfg.team_id} ({existing} already present, {failed} failed) in {seconds:.2f} s; not part of the measured run.")
    return {"team_id": cfg.team_id, "registered": registered, "already_present": existing, "failed": failed, "seconds": seconds}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--athletes", type=int, default=DEFAULT_ATHLETES, help=f"number of fictional athletes (default {DEFAULT_ATHLETES})")
    p.add_argument("--duration", type=float, default=DEFAULT_DURATION_S, help=f"approximate run length in seconds (default {DEFAULT_DURATION_S:g})")
    p.add_argument("--rate", type=float, default=DEFAULT_RATE_HZ, help=f"samples per second per athlete for both streams (default {DEFAULT_RATE_HZ:g}); a per-stream option below overrides it for that stream")
    p.add_argument("--physiological-rate", type=float, default=None, help="physiological samples per second per athlete (overrides --rate for this stream)")
    p.add_argument("--tracking-rate", type=float, default=None, help="tracking samples per second per athlete (overrides --rate for this stream)")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S, help=f"per-request timeout in seconds (default {DEFAULT_TIMEOUT_S:g})")
    p.add_argument("--workers", type=int, default=8, help="concurrent request workers (threads) (default 8)")
    p.add_argument("--report", type=Path, default=None, help="JSON report path (default reports/team-scale-<UTC time>.json, ignored by git)")
    p.add_argument("--port", type=int, default=None, help="port for the isolated backend (default: a free port)")
    p.add_argument("--read-interval", type=float, default=DEFAULT_READ_INTERVAL_S, help=f"seconds between overview reads during ingestion; 0 disables them (default {DEFAULT_READ_INTERVAL_S:g})")
    p.add_argument("--scenario", choices=("healthy", "interrupted"), default="healthy", help="healthy: every sample on time; interrupted: paused tracking streams, late physiological samples and device statuses for a few athletes (needs >= 3 athletes and >= 13 ticks)")
    return p


def main(argv: list[str] | None = None, *, out: Callable[[str], None] = print) -> int:
    args = build_parser().parse_args(argv)
    if args.athletes < 1 or args.duration <= 0 or args.rate <= 0 or args.timeout <= 0 or any(v is not None and v <= 0 for v in (args.physiological_rate, args.tracking_rate)):
        out("athletes must be >= 1 and duration, rates and timeout must be positive")
        return 2
    cfg = RunConfig(athletes=args.athletes, duration_s=args.duration, rate_hz=args.rate, timeout_s=args.timeout, workers=args.workers, read_interval_s=max(0.0, args.read_interval), scenario=args.scenario,
                    physiological_rate_hz=args.physiological_rate, tracking_rate_hz=args.tracking_rate)
    try:
        plan = scenario_plan(cfg.scenario, cfg.athletes, cfg)
    except ValueError as exc:
        out(str(exc))
        return 2
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
        setup = register_team(backend.base_url, cfg, out)
        out(f"Sending {cfg.expected_samples} fictional samples for {cfg.athletes} athletes (physiological {cfg.rate_for('physiological'):g} Hz, tracking {cfg.rate_for('tracking'):g} Hz)" + (f", scenario {cfg.scenario}" if cfg.scenario != "healthy" else "") + (f", overview read every {cfg.read_interval_s:g} s" if cfg.read_interval_s > 0 else "") + "…")
        result = run_load(backend.base_url, cfg, plan=plan)
        records, actual_s, start_utc = result.records, result.actual_s, result.start_utc
        out("Reading back and verifying…")
        with httpx.Client(timeout=cfg.timeout_s) as client:
            verification = verify(client, backend.base_url, records, cfg, start_utc)
            overview_verification = verify_overview(client, backend.base_url, records, result.statuses, cfg, plan)
        report = build_report(cfg, backend_info, records, actual_s, start_utc, verification, reads=result.reads, statuses=result.statuses, setup=setup, overview_verification=overview_verification, plan=plan)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print_summary(report, out)
        out(f"JSON report: {report_path}")
        v = report["verification"]["overall"]
        ov_ok = all(x is True for x in report["overview_verification"].get("checks", {}).values())   # unknown is not a pass
        reads_ok = not report["reads"]["checks_failed"] and report["reads"]["unsent"] == 0
        return 0 if v["failed"] == 0 and report["requests"]["unsent"] == 0 and ov_ok and reads_ok else 1
    finally:
        if not stopping["done"]:
            stopping["done"] = True
            backend.cleanup()
        for s, h in previous.items():
            signal.signal(s, h)


if __name__ == "__main__":
    sys.exit(main())
