"""Repeatable local validation of the engine with many fictional athletes sending samples concurrently.

WHAT IT DOES
  1. Starts its own isolated backend: a fresh temporary SQLite database and a free localhost port. The normal
     development backend and database are never touched, and only the process this tool started is stopped.
  2. Sends, for every fictional athlete, one physiological and one tracking sample per tick at the requested
     rate (default 1 Hz) for the requested duration (default 15 s), scheduled on a monotonic clock. Every
     request records when it was due, when it was actually sent, how long the round trip took and how it
     ended. Failures are counted, never retried, and never replaced by invented successes.
  3. Reads back what the backend retained and checks it against the samples that were accepted: per-athlete
     counts (under each stream's cap, default 300; --physiological-cap / --tracking-cap start the isolated
     backend with other caps through its own environment, never .env), the reported caps, latest timestamps, athlete and device separation, both streams in one
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
    python scripts/validate_team_scale.py --athletes 20 --duration 60 --physiological-rate 1 --tracking-rate 10 --tracking-cap 3000
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
DEFAULT_RETENTION_CAP = 300          # the backend's default per-stream cap; --physiological-cap / --tracking-cap set the isolated backend's own
RETENTION_CAP = DEFAULT_RETENTION_CAP  # kept for older callers: the default, not necessarily a run's configured cap
HISTORY_QUERY_LIMIT = 300            # the history endpoint's page-size limit, separate from the retention caps
STREAMS = ("physiological", "tracking")
SOURCE = "simulated"
MAX_WINDOW_S = 300                   # the window endpoint's longest request, separate from the retention caps


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
    physiological_cap: int = DEFAULT_RETENTION_CAP   # retention caps the isolated backend is started with (and verified against)
    tracking_cap: int = DEFAULT_RETENTION_CAP
    recording_id: str | None = None             # an active recording covering the athletes (--record); session reads use it
    session_read_interval_s: float = 0.0        # occasional session window reads of the active recording; 0 disables them
    athlete_read_interval_s: float = 0.0        # individual coherent live reads (GET /athletes/{id}/live), rotating athletes; 0 disables them

    def cap_for(self, stream: str) -> int:
        return self.physiological_cap if stream == "physiological" else self.tracking_cap

    @property
    def retention_caps(self) -> dict[str, int]:
        return {s: self.cap_for(s) for s in STREAMS}

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
    session_reads: list = field(default_factory=list)   # ReadRecord per session window read of the active recording
    athlete_reads: list = field(default_factory=list)   # ReadRecord per individual live read


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
    if cfg.session_read_interval_s > 0 and cfg.recording_id:
        for k in range(int(cfg.duration_s // cfg.session_read_interval_s) + 1):
            if k * cfg.session_read_interval_s > 0:
                events.append((k * cfg.session_read_interval_s, 4, "session_read", (k,)))
    if cfg.athlete_read_interval_s > 0:
        for k in range(int(cfg.duration_s // cfg.athlete_read_interval_s) + 1):
            if k * cfg.athlete_read_interval_s > 0:
                events.append((k * cfg.athlete_read_interval_s, 5, "athlete_read", (k,)))
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

    session_reads: list[ReadRecord] = []
    athlete_reads: list[ReadRecord] = []
    athlete_previous: dict = {}

    def athlete_read_job(k: int, due: float) -> None:
        """GET one athlete's coherent live snapshot (rotating): it must name that athlete, never show a negative age, and never go backwards."""
        a = athlete_id(k % cfg.athletes)
        if clock() > deadline:
            rec = ReadRecord(k, due, None, None, "unsent", None, {}, "deadline passed")
        else:
            sent = clock(); t0 = clock()
            try:
                r = client.get(f"{base_url}/api/v1/athletes/{a}/live")
                latency = clock() - t0
                if r.status_code == 200:
                    body = r.json()
                    checks = {"names_this_athlete": body.get("athlete_id") == a and all((body.get(s) or {}).get("athlete_id", a) == a for s in STREAMS),
                              "ages_never_negative": all((body.get(f"{s}_age_seconds") is None) or body[f"{s}_age_seconds"] >= 0 for s in STREAMS)}
                    with lock:
                        prev = athlete_previous.get(a, {})
                        ok = True
                        for s in STREAMS:
                            ts = (body.get(s) or {}).get("timestamp")
                            if ts:
                                t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                                if s in prev and t < prev[s]:
                                    ok = False
                                prev[s] = t
                        athlete_previous[a] = prev
                    checks["latest_never_goes_backwards"] = ok
                    rec = ReadRecord(k, due, sent - start, latency, "ok", 200, checks, a)
                else:
                    rec = ReadRecord(k, due, sent - start, latency, "http_error", r.status_code, {}, r.text[:200])
            except httpx.TimeoutException as exc:
                rec = ReadRecord(k, due, sent - start, clock() - t0, "timeout", None, {}, type(exc).__name__)
            except httpx.HTTPError as exc:
                rec = ReadRecord(k, due, sent - start, clock() - t0, "connection_error", None, {}, type(exc).__name__)
        with lock:
            athlete_reads.append(rec)

    def session_read_job(k: int, due: float) -> None:
        """GET the active recording's 60 s window for one athlete (rotating): samples must name that athlete only."""
        a = athlete_id(k % cfg.athletes)
        if clock() > deadline:
            rec = ReadRecord(k, due, None, None, "unsent", None, {}, "deadline passed")
        else:
            sent = clock(); t0 = clock()
            try:
                r = client.get(f"{base_url}/api/v1/recordings/{cfg.recording_id}/athletes/{a}/window", params={"seconds": 60})
                latency = clock() - t0
                if r.status_code == 200:
                    body = r.json()
                    sa = body.get("session_availability") or {}
                    checks = {"only_this_athletes_samples": all(x["athlete_id"] == a for x in body["physiological_samples"] + body["tracking_samples"]),
                              "selected_counts_match_samples": sa.get("tracking", {}).get("selected_count") == len(body["tracking_samples"]) and sa.get("physiological", {}).get("selected_count") == len(body["physiological_samples"])}
                    rec = ReadRecord(k, due, sent - start, latency, "ok", 200, checks, a)
                else:
                    rec = ReadRecord(k, due, sent - start, latency, "http_error", r.status_code, {}, r.text[:200])
            except httpx.TimeoutException as exc:
                rec = ReadRecord(k, due, sent - start, clock() - t0, "timeout", None, {}, type(exc).__name__)
            except httpx.HTTPError as exc:
                rec = ReadRecord(k, due, sent - start, clock() - t0, "connection_error", None, {}, type(exc).__name__)
        with lock:
            session_reads.append(rec)

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
                elif kind == "session_read":
                    futures.append(pool.submit(session_read_job, args[0], due))
                elif kind == "athlete_read":
                    futures.append(pool.submit(athlete_read_job, args[0], due))
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
    session_reads.sort(key=lambda r: r.tick)
    athlete_reads.sort(key=lambda r: r.tick)
    return RunResult(records, reads, statuses, actual, start_utc, plan, session_reads, athlete_reads)


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


def expected_retained(records: list[RequestRecord], athlete: str, stream: str, cap: int = DEFAULT_RETENTION_CAP) -> list[RequestRecord]:
    """Accepted samples for the athlete and stream, the newest `cap` by timestamp (what the backend should retain under that stream's cap)."""
    acc = [r for r in records if r.athlete == athlete and r.stream == stream and r.outcome == "accepted"]
    acc.sort(key=lambda r: r.payload["timestamp"])
    return acc[-cap:]


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
            exp = {s: expected_retained(records, a, s, cfg.cap_for(s)) for s in STREAMS}
            for s in STREAMS:
                got = retained[s]["sample_count"]
                checks[f"{s}_retained_count"] = got == len(exp[s])
                checks[f"{s}_reported_cap"] = retained[s].get("max_samples") == cfg.cap_for(s)   # the backend reports the cap it was started with
                if exp[s]:
                    latest = client.get(f"{base_url}/api/v1/athletes/{a}/{s}/latest").json()
                    checks[f"{s}_latest_timestamp"] = datetime.fromisoformat(latest["timestamp"].replace("Z", "+00:00")) == datetime.fromisoformat(exp[s][-1].payload["timestamp"])
                    checks[f"{s}_latest_athlete_and_device"] = latest["athlete_id"] == a and latest["device_id"] == device_id(i, s)
                    seen_devices.setdefault(a, set()).add(latest["device_id"])
                    history = client.get(f"{base_url}/api/v1/athletes/{a}/{s}/history?limit={min(cfg.cap_for(s), HISTORY_QUERY_LIMIT)}").json()
                    checks[f"{s}_history_only_this_athlete"] = all(h["athlete_id"] == a for h in history)
            spans = {}
            for s in STREAMS:
                lo, hi = retained[s]["earliest_timestamp"], retained[s]["latest_timestamp"]
                spans[s] = {"retained": retained[s]["sample_count"], "span_s": (datetime.fromisoformat(hi.replace("Z", "+00:00")) - datetime.fromisoformat(lo.replace("Z", "+00:00"))).total_seconds() if lo and hi else None,
                            "cap": cfg.cap_for(s), "capped": retained[s]["sample_count"] >= cfg.cap_for(s)}
            notes.append("retained: " + "; ".join(f"{s} {v['retained']} rows spanning {v['span_s']} s" + (f" (at the {v['cap']}-row cap)" if v["capped"] else "") for s, v in spans.items()))
            if exp["physiological"] or exp["tracking"]:
                last_ts = max((r.payload["timestamp"] for s in STREAMS for r in exp[s]), key=lambda t: datetime.fromisoformat(t))
                window_s = min(MAX_WINDOW_S, max(1, int(cfg.duration_s) + 1))   # the window endpoint's own limit, not a retention cap
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

    def __init__(self, port: int | None = None, log_level: str = "warning", cors_origins: list[str] | None = None, retention_caps: dict[str, int] | None = None) -> None:
        self.port = port or free_port()
        self.cors_origins = cors_origins or ["http://localhost:3000"]
        self.retention_caps = dict(retention_caps or {})   # per-stream caps for this backend only, passed as environment variables; .env is untouched
        for stream, cap in self.retention_caps.items():
            if stream not in STREAMS or isinstance(cap, bool) or not isinstance(cap, int) or cap < 1:
                raise ValueError(f"retention cap for {stream!r} must be a positive integer, got {cap!r}")
        self.tmp = tempfile.TemporaryDirectory(prefix="athletrix-team-scale-")
        self.db_path = Path(self.tmp.name) / "isolated.db"
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.cmd = [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(self.port), "--log-level", log_level]
        self.proc: subprocess.Popen | None = None
        self.log_path = Path(self.tmp.name) / "backend.log"

    def start(self, ready_timeout_s: float = 20.0) -> None:
        env = {**os.environ, "DATABASE_PATH": str(self.db_path), "CORS_ORIGINS": ",".join(self.cors_origins), **self.environment()}  # only this isolated backend's CORS and caps
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

    def environment(self) -> dict[str, str]:
        """The retention settings this backend is started with (empty: the backend's own defaults)."""
        return {f"{stream.upper()}_RETENTION_SAMPLES": str(cap) for stream, cap in self.retention_caps.items()}

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
                   "retention_caps": cfg.retention_caps,
                   "retention_cap_note": "each cap is a row count per stream, not a duration: 300 rows span 30 s at 10 Hz and 300 s at 1 Hz; 3000 rows span about five minutes only at 10 Hz"},
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
    caps = c.get("retention_caps", {})
    out(f"Config: {c['athletes']} athletes · physiological {c['physiological_rate_hz']:g} Hz ({c['ticks']['physiological']} ticks, cap {caps.get('physiological', DEFAULT_RETENTION_CAP)}) · tracking {c['tracking_rate_hz']:g} Hz ({c['ticks']['tracking']} ticks, cap {caps.get('tracking', DEFAULT_RETENTION_CAP)}) · requested {t['requested_duration_s']:.1f} s · actual run {t['actual_run_duration_s']:.2f} s (start to last response) · timeout {c['request_timeout_s']:g} s")
    out(f"Sample requests: expected {r['expected']} · attempted {r['attempted']} · accepted {r['accepted']} · failed {r['failed']} (http {r['http_error']}, timeout {r['timeout']}, connection {r['connection_error']}) · unsent {r['unsent']} · omitted by scenario {r['omitted_by_scenario']}")
    for s in STREAMS:
        b = report["streams"][s]
        rs = report.get("retained_spans", {}).get(s, {})
        spans = [x for x in rs.get("span_s", []) if x is not None]
        out(f"  {s:14s} requested {b['requested_rate_hz']:g} Hz · expected {b['expected']} · attempted {b['attempted']} · accepted {b['accepted']} · failed {b['failed']} · unknown (timeout) {b['unknown_timeout']} · omitted by scenario {b['omitted_by_scenario']} · unsent {b['unsent']}")
        out(f"  {'':14s} delivered {b['delivered_rate_hz'] if b['delivered_rate_hz'] is None else round(b['delivered_rate_hz'], 3)} Hz over a {b['measurement_interval_s']:.1f} s schedule span · latency p50 {f(b['latency_s']['p50'])} p95 {f(b['latency_s']['p95'])} max {f(b['latency_s']['max'])} · lateness p50 {f(b['scheduling_lateness_s']['p50'])} p95 {f(b['scheduling_lateness_s']['p95'])} max {f(b['scheduling_lateness_s']['max'])}")
        if rs.get("rows"):
            cap = c.get("retention_caps", {}).get(s, DEFAULT_RETENTION_CAP)
            out(f"  {'':14s} retained per athlete: {min(rs['rows'])}-{max(rs['rows'])} rows spanning {min(spans):.1f}-{max(spans):.1f} s" + (f" (at the {cap}-row cap: a count, not a duration)" if max(rs['rows']) >= cap else f" (cap {cap} rows)"))
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


# ---- active recording: sizes, rolling versus recorded, restart, effort reviews ----------------------------------------------

def database_bytes(db_path: Path) -> int:
    """The SQLite file plus its WAL and shared-memory sidecars, in bytes (what the run actually occupies on disk)."""
    return sum((db_path.parent / (db_path.name + suffix)).stat().st_size for suffix in ("", "-wal", "-shm") if (db_path.parent / (db_path.name + suffix)).exists())


class SizeSampler:
    """Records the database size at fixed seconds of wall time during the run (a background thread owned by this run)."""

    def __init__(self, db_path: Path, interval_s: float) -> None:
        self.db_path, self.interval_s = db_path, interval_s
        self.points: list[dict] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._t0 = time.monotonic()

    def mark(self, label: str) -> None:
        self.points.append({"label": label, "elapsed_s": round(time.monotonic() - self._t0, 1), "bytes": database_bytes(self.db_path)})

    def _loop(self) -> None:
        n = 1
        while not self._stop.wait(max(0.0, self._t0 + n * self.interval_s - time.monotonic())):
            self.mark(f"during the run, {n * self.interval_s:g} s")
            n += 1

    def start(self) -> None:
        if self.interval_s > 0:
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5)


def recorded_snapshot(client: httpx.Client, base_url: str, rec_id: str, cfg: RunConfig) -> dict:
    """Per athlete and stream: rows the recording holds and their timestamp span, plus the rolling store's rows and span."""
    out = {}
    for i in range(cfg.athletes):
        a = athlete_id(i)
        rd = client.get(f"{base_url}/api/v1/recordings/{rec_id}/athletes/{a}/recorded-data").json()
        rt = client.get(f"{base_url}/api/v1/athletes/{a}/retained-data").json()
        def span(x):
            lo, hi = x.get("earliest_timestamp"), x.get("latest_timestamp")
            return (datetime.fromisoformat(hi.replace("Z", "+00:00")) - datetime.fromisoformat(lo.replace("Z", "+00:00"))).total_seconds() if lo and hi else None
        out[a] = {s: {"recorded": rd[s]["sample_count"], "recorded_span_s": span(rd[s]), "rolling": rt[s]["sample_count"], "rolling_span_s": span(rt[s])} for s in STREAMS}
    return out


def effort_reviews(client: httpx.Client, base_url: str, rec_id: str, cfg: RunConfig, records: list, start_utc: datetime) -> list[dict]:
    """Early, middle and late 20 s effort reviews of the first athlete over the recording, each cross-checked independently."""
    from compare_retention import close, independent_movement  # local import: compare_retention imports this module
    a = athlete_id(0)
    accepted = {}
    for r in records:
        if r.athlete == a and r.stream == "tracking" and r.outcome == "accepted":
            accepted[datetime.fromisoformat(r.payload["timestamp"])] = r
    timed_out = any(r.athlete == a and r.stream == "tracking" and r.outcome == "timeout" for r in records)
    out = []
    length = min(20.0, max(1.0, cfg.duration_s / 3))                 # 20 s reviews, shorter only for very short runs
    places = (("early", min(10.0, cfg.duration_s / 6)), ("middle", (cfg.duration_s - length) / 2), ("late", max(0.0, cfg.duration_s - length - 20.0)))
    for name, lo in places:
        start, end = start_utc + timedelta(seconds=lo), start_utc + timedelta(seconds=lo + length)
        resp = client.get(f"{base_url}/api/v1/recordings/{rec_id}/athletes/{a}/effort-review", params={"effort_start": start.isoformat(), "effort_end": end.isoformat()})
        if resp.status_code != 200:
            out.append({"name": name, "effort_start": start.isoformat(), "effort_end": end.isoformat(), "tracking_samples": None, "distance_m": None, "high_speed_s": None,
                        "heart_rate_during": None, "matches_independent_calculation": False, "detail": f"HTTP {resp.status_code}: {resp.text[:200]}"})
            continue
        review = resp.json()
        exp = independent_movement([r for t, r in sorted(accepted.items()) if start <= t <= end])
        m = review["movement"]
        ok = m["sample_count"] == exp["sample_count"] and close(m["estimated_distance_m"], exp["estimated_distance_m"]) and close(m["high_speed_duration_seconds"], exp["high_speed_duration_seconds"])
        out.append({"name": name, "effort_start": start.isoformat(), "effort_end": end.isoformat(), "tracking_samples": m["sample_count"], "distance_m": m["estimated_distance_m"],
                    "high_speed_s": m["high_speed_duration_seconds"], "heart_rate_during": {"samples": review["heart_rate"]["during"]["sample_count"], "mean_bpm": review["heart_rate"]["during"]["heart_rate_mean_bpm"]},
                    "matches_independent_calculation": (None if (not ok and timed_out) else ok)})
    return out


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
    p.add_argument("--physiological-cap", type=int, default=DEFAULT_RETENTION_CAP, help=f"newest physiological samples the isolated backend keeps per athlete (default {DEFAULT_RETENTION_CAP}; its own environment only, .env is untouched)")
    p.add_argument("--tracking-cap", type=int, default=DEFAULT_RETENTION_CAP, help=f"newest tracking samples the isolated backend keeps per athlete (default {DEFAULT_RETENTION_CAP}; a count, not a time span: 3000 is about five minutes only at 10 Hz)")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S, help=f"per-request timeout in seconds (default {DEFAULT_TIMEOUT_S:g})")
    p.add_argument("--workers", type=int, default=8, help="concurrent request workers (threads) (default 8)")
    p.add_argument("--report", type=Path, default=None, help="JSON report path (default reports/team-scale-<UTC time>.json, ignored by git)")
    p.add_argument("--port", type=int, default=None, help="port for the isolated backend (default: a free port)")
    p.add_argument("--read-interval", type=float, default=DEFAULT_READ_INTERVAL_S, help=f"seconds between overview reads during ingestion; 0 disables them (default {DEFAULT_READ_INTERVAL_S:g})")
    p.add_argument("--record", action="store_true", help="start a recording covering every athlete before the run, stop it afterwards, and report rolling versus recorded data, database sizes, a restart check and early/middle/late effort reviews")
    p.add_argument("--session-read-interval", type=float, default=15.0, help="with --record: seconds between session window reads of the active recording; 0 disables them (default 15)")
    p.add_argument("--athlete-read-interval", type=float, default=0.0, help="seconds between individual live reads (GET /athletes/{id}/live, rotating athletes) during the run; 0 disables them (default 0)")
    p.add_argument("--size-interval", type=float, default=60.0, help="with --record: seconds between database size checkpoints during the run (default 60)")
    p.add_argument("--scenario", choices=("healthy", "interrupted"), default="healthy", help="healthy: every sample on time; interrupted: paused tracking streams, late physiological samples and device statuses for a few athletes (needs >= 3 athletes and >= 13 ticks)")
    return p


def main(argv: list[str] | None = None, *, out: Callable[[str], None] = print) -> int:
    args = build_parser().parse_args(argv)
    if args.athletes < 1 or args.duration <= 0 or args.rate <= 0 or args.timeout <= 0 or any(v is not None and v <= 0 for v in (args.physiological_rate, args.tracking_rate)):
        out("athletes must be >= 1 and duration, rates and timeout must be positive")
        return 2
    if args.physiological_cap < 1 or args.tracking_cap < 1:
        out("retention caps must be positive integers")
        return 2
    cfg = RunConfig(athletes=args.athletes, duration_s=args.duration, rate_hz=args.rate, timeout_s=args.timeout, workers=args.workers, read_interval_s=max(0.0, args.read_interval), scenario=args.scenario, athlete_read_interval_s=max(0.0, args.athlete_read_interval),
                    physiological_rate_hz=args.physiological_rate, tracking_rate_hz=args.tracking_rate, physiological_cap=args.physiological_cap, tracking_cap=args.tracking_cap)
    try:
        plan = scenario_plan(cfg.scenario, cfg.athletes, cfg)
    except ValueError as exc:
        out(str(exc))
        return 2
    report_path = args.report or (ROOT / "reports" / f"team-scale-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json")
    backend = IsolatedBackend(port=args.port, retention_caps=cfg.retention_caps)
    stopping = {"done": False}

    def on_signal(signum, _frame):  # stop only the backend we started, then exit
        if not stopping["done"]:
            stopping["done"] = True
            backend.cleanup()
        raise SystemExit(130)

    previous = {s: signal.signal(s, on_signal) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        out(f"Starting an isolated backend on port {backend.port} with database {backend.db_path} and retention caps physiological {cfg.physiological_cap} / tracking {cfg.tracking_cap} (the development backend is not involved)…")
        backend.start()
        backend_info = {"base_url": backend.base_url, "pid": backend.proc.pid if backend.proc else None, "db_path": str(backend.db_path), "command": backend.cmd, "retention_caps": cfg.retention_caps, "retention_cap": cfg.physiological_cap if cfg.physiological_cap == cfg.tracking_cap else None}
        setup = register_team(backend.base_url, cfg, out)
        sizes = None
        if args.record:
            with httpx.Client(timeout=cfg.timeout_s) as c:
                r = c.post(f"{backend.base_url}/api/v1/recordings", json={"name": "Fictional validation session", "athlete_ids": [athlete_id(i) for i in range(cfg.athletes)]})
            if r.status_code != 201:
                out(f"Could not start the recording: HTTP {r.status_code} {r.text[:200]}")
                return 1
            cfg.recording_id = r.json()["id"]; cfg.session_read_interval_s = max(0.0, args.session_read_interval)
            sizes = SizeSampler(backend.db_path, args.size_interval)
            sizes.mark("after registration, recording started")
            sizes.start()
            out(f"Recording {cfg.recording_id} is active for all {cfg.athletes} athletes; session window reads every {cfg.session_read_interval_s:g} s; database size every {args.size_interval:g} s.")
        out(f"Sending {cfg.expected_samples} fictional samples for {cfg.athletes} athletes (physiological {cfg.rate_for('physiological'):g} Hz, tracking {cfg.rate_for('tracking'):g} Hz)" + (f", scenario {cfg.scenario}" if cfg.scenario != "healthy" else "") + (f", overview read every {cfg.read_interval_s:g} s" if cfg.read_interval_s > 0 else "") + "…")
        try:
            result = run_load(backend.base_url, cfg, plan=plan)
        finally:
            if sizes is not None:
                sizes.stop()
        records, actual_s, start_utc = result.records, result.actual_s, result.start_utc
        out("Reading back and verifying…")
        with httpx.Client(timeout=cfg.timeout_s) as client:
            verification = verify(client, backend.base_url, records, cfg, start_utc)
            overview_verification = verify_overview(client, backend.base_url, records, result.statuses, cfg, plan)
        report = build_report(cfg, backend_info, records, actual_s, start_utc, verification, reads=result.reads, statuses=result.statuses, setup=setup, overview_verification=overview_verification, plan=plan)
        ar = result.athlete_reads
        report["athlete_live_reads"] = {"interval_s": cfg.athlete_read_interval_s, "expected": len(ar), "ok": sum(1 for x in ar if x.outcome == "ok"), "failed": sum(1 for x in ar if x.outcome not in ("ok", "unsent")),
                                        "timeout": sum(1 for x in ar if x.outcome == "timeout"), "unsent": sum(1 for x in ar if x.outcome == "unsent"),
                                        "checks_failed": sorted({k for x in ar for k, v in x.checks.items() if v is False}), "latency_s": summarize([x.latency_s for x in ar if x.latency_s is not None])}
        if args.record:
            out("Stopping the recording, comparing rolling and recorded data, restarting the backend and reviewing early, middle and late efforts…")
            sizes.mark("end of the run")
            with httpx.Client(timeout=30.0) as c:
                stopped = c.post(f"{backend.base_url}/api/v1/recordings/{cfg.recording_id}/stop").json()
                sizes.mark("recording stopped")
                before = recorded_snapshot(c, backend.base_url, cfg.recording_id, cfg)
            backend.stop(); backend.start()
            sizes.mark("after a backend restart")
            with httpx.Client(timeout=30.0) as c:
                after = recorded_snapshot(c, backend.base_url, cfg.recording_id, cfg)
                reviews = effort_reviews(c, backend.base_url, cfg.recording_id, cfg, records, start_utc)
            accepted = {s: {} for s in STREAMS}
            for r in records:
                if r.outcome == "accepted":
                    accepted[r.stream][r.athlete] = accepted[r.stream].get(r.athlete, 0) + 1
            timed_out = {s: {r.athlete for r in records if r.stream == s and r.outcome == "timeout"} for s in STREAMS}
            ok_counts = True
            for a, v in before.items():
                for st in STREAMS:
                    if v[st]["recorded"] != accepted[st].get(a, 0) and a not in timed_out[st]:
                        ok_counts = False
            sr = result.session_reads
            report["recording"] = {
                "id": cfg.recording_id, "started_and_stopped": {"started_at": stopped.get("started_at"), "stopped_at": stopped.get("stopped_at")},
                "recorded_equals_accepted_per_athlete": ok_counts,
                "survives_restart": before == after,
                "per_stream": {st: {"recorded_rows_min": min(v[st]["recorded"] for v in before.values()), "recorded_rows_max": max(v[st]["recorded"] for v in before.values()),
                                    "recorded_span_s_min": min((v[st]["recorded_span_s"] or 0) for v in before.values()), "recorded_span_s_max": max((v[st]["recorded_span_s"] or 0) for v in before.values()),
                                    "rolling_rows_min": min(v[st]["rolling"] for v in before.values()), "rolling_rows_max": max(v[st]["rolling"] for v in before.values()),
                                    "rolling_span_s_min": min((v[st]["rolling_span_s"] or 0) for v in before.values()), "rolling_span_s_max": max((v[st]["rolling_span_s"] or 0) for v in before.values())} for st in STREAMS},
                "per_athlete": before,
                "database_bytes": sizes.points,
                "session_reads": {"expected": len(sr), "ok": sum(1 for x in sr if x.outcome == "ok"), "failed": sum(1 for x in sr if x.outcome not in ("ok", "unsent")), "unsent": sum(1 for x in sr if x.outcome == "unsent"),
                                  "checks_failed": sorted({k for x in sr for k, v in x.checks.items() if v is False}), "latency_s": summarize([x.latency_s for x in sr if x.latency_s is not None])},
                "effort_reviews": reviews,
            }
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print_summary(report, out)
        out(f"JSON report: {report_path}")
        v = report["verification"]["overall"]
        al = report["athlete_live_reads"]
        if al["expected"]:
            g = lambda x: "n/a" if x is None else f"{x * 1000:.1f} ms"  # noqa: E731
            out(f"Individual live reads (GET /athletes/{{id}}/live every {al['interval_s']:g} s, rotating): {al['ok']}/{al['expected']} ok, {al['failed']} failed (timeout {al['timeout']}), {al['unsent']} unsent, checks failed {al['checks_failed'] or 'none'}, latency p50 {g(al['latency_s']['p50'])} p95 {g(al['latency_s']['p95'])} max {g(al['latency_s']['max'])}")
            report_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        if args.record:
            rr = report["recording"]
            f = lambda x: "n/a" if x is None else f"{x * 1000:.1f} ms"  # noqa: E731
            out(f"Recording {rr['id']}: recorded rows equal accepted samples per athlete: {rr['recorded_equals_accepted_per_athlete']} · identical after a backend restart: {rr['survives_restart']}")
            for st, ps in rr["per_stream"].items():
                out(f"  {st:14s} recorded per athlete {ps['recorded_rows_min']}-{ps['recorded_rows_max']} rows spanning {ps['recorded_span_s_min']:.1f}-{ps['recorded_span_s_max']:.1f} s · rolling {ps['rolling_rows_min']}-{ps['rolling_rows_max']} rows spanning {ps['rolling_span_s_min']:.1f}-{ps['rolling_span_s_max']:.1f} s")
            sr = rr["session_reads"]
            out(f"  session window reads: {sr['ok']}/{sr['expected']} ok, {sr['failed']} failed, {sr['unsent']} unsent, checks failed {sr['checks_failed'] or 'none'}, latency p50 {f(sr['latency_s']['p50'])} p95 {f(sr['latency_s']['p95'])} max {f(sr['latency_s']['max'])}")
            out("  database size: " + " · ".join(f"{p['label']} {p['bytes'] / 1e6:.1f} MB" for p in rr["database_bytes"]))
            for e in rr["effort_reviews"]:
                out(f"  {e['name']:6s} effort {e['effort_start']} to {e['effort_end']}: {e['tracking_samples']} tracking samples, distance {e['distance_m'] if e['distance_m'] is None else round(e['distance_m'], 2)} m, high-speed {e['high_speed_s'] if e['high_speed_s'] is None else round(e['high_speed_s'], 2)} s, heart rate during {e['heart_rate_during']} · matches independent calculation: {e['matches_independent_calculation']}")
            report_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        ov_ok = all(x is True for x in report["overview_verification"].get("checks", {}).values())   # unknown is not a pass
        reads_ok = not report["reads"]["checks_failed"] and report["reads"]["unsent"] == 0
        rec_ok = not report["athlete_live_reads"]["checks_failed"] and report["athlete_live_reads"]["unsent"] == 0 and report["athlete_live_reads"]["failed"] == 0
        if args.record:
            rr = report["recording"]
            rec_ok = rec_ok and rr["recorded_equals_accepted_per_athlete"] and rr["survives_restart"] and not rr["session_reads"]["checks_failed"] and all(e["matches_independent_calculation"] is True for e in rr["effort_reviews"])
        return 0 if v["failed"] == 0 and report["requests"]["unsent"] == 0 and ov_ok and reads_ok and rec_ok else 1
    finally:
        if not stopping["done"]:
            stopping["done"] = True
            backend.cleanup()
        for s, h in previous.items():
            signal.signal(s, h)


if __name__ == "__main__":
    sys.exit(main())
