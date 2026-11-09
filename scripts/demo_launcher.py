"""One-command isolated demo of the local Athletrix prototype.

DEFAULT (since 2026-11-09): --session pilot, the FICTIONAL fatigue workflow (the product path: wearable and camera
readings -> the Fatigue estimate card -> a saved pre-sprint estimate -> independent timing -> export, reproduction and
the three-way report). --session simple keeps the old sine-wave feed; --session match is legacy.

Starts a throwaway copy of everything a demonstration needs, without touching the normal development setup:

  1. a fresh temporary SQLite database and its own backend (uvicorn app.main:app) on a free localhost port;
  2. a copy of the existing static frontend served on a second free localhost port, with that copy (and only that
     copy) pointed at the demo backend; the repository's files are never edited;
  3. a clearly fictional multi-sport demo roster registered through the API;
  4. a small team fed in-process with clearly simulated physiological and tracking samples (and, with
     --device-status, fictional device-status reports) for a bounded time, reusing the simulator's deterministic
     profiles rather than a second simulator;
  5. the exact dashboard URL and short instructions on the terminal.

When the bounded data generation ends the servers stay up (until Ctrl-C) so the dashboard can still be explored:
the live cards then show the retained readings as "stopped updating", Recorded mode can review them, and nothing is
rebased or replayed to look live. Nothing is uploaded anywhere.

The launcher never edits tracked frontend or configuration files, never changes .env, never touches the development
database, never takes over an occupied port or stops unrelated processes, never changes hosting or DNS, and installs
nothing. CORS is configured only for its own backend and only for its own frontend port. On normal exit, failure or
Ctrl-C it stops only the two processes it started and removes only the temporary folder it created.

SESSION MODE (--session match): instead of the simple sine-wave feed, a paced FICTIONAL match session
(scripts/live_session.py): a pre-match rest recording and accepted fictional baselines, a roster with starters and
bench players, coach context and match constraints, physiology and performance generated independently, and phases
(maintained performance, a decline, a tactical change, missing data, recovery, a replacement option becoming
unavailable, simulated coach responses). Only this demo backend gets the FICTIONAL live profiles (its own
environment); the normal engine still starts with no profile. It is a paced script, not live capture.

USAGE
    python scripts/demo_launcher.py --session match                   # the fictional match session (about 6 minutes)
    python scripts/demo_launcher.py --session match --speed 3 --duration 360 --exit-after-demo
    python scripts/demo_launcher.py --session pilot                   # the FICTIONAL fatigue-pilot rehearsal with the dashboard (about 13 minutes)
    python scripts/demo_launcher.py                                  # frontend folder guessed next to this repository
    python scripts/demo_launcher.py --frontend "/path/to/Athletrix Fixed Website" --athletes 4 --duration 90 --device-status
    python scripts/demo_launcher.py --exit-after-demo                 # stop everything when data generation ends
    python scripts/demo_launcher.py --tracking-cap 3000               # the demo backend keeps 3000 tracking samples per athlete
"""

import argparse
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
for p in (str(ROOT), str(SCRIPTS)):
    if p not in sys.path:
        sys.path.insert(0, p)

from simulate_athlete import heart_rate_at, motion_at  # noqa: E402  (deterministic fictional profiles)
from validate_team_scale import IsolatedBackend, free_port  # noqa: E402  (isolated backend process handling)

DEFAULT_FRONTEND = ROOT.parent / "Athletrix Fixed Website"
DEFAULT_ATHLETES = 4
DEFAULT_DURATION_S = 90
DEFAULT_RATE_HZ = 1.0
STATUS_EVERY_S = 5
TEAM_ID = "demo-team"
SPORTS = ["football", "basketball", "hockey", "rugby", "handball", "futsal"]
API_BASE_PLACEHOLDER = '"http://127.0.0.1:8000"'   # the default apiBase in the frontend, replaced in the served copy only


def say(text: str) -> None:
    print(text, flush=True)   # the launcher is often run with its output redirected; keep the URL and progress visible at once


# ---- fictional roster and samples ------------------------------------------------------------------------------

def demo_roster(count: int) -> list[dict]:
    """Clearly fictional athletes across several sports, all on one demo team."""
    return [{"id": f"demo-athlete-{i + 1:02d}", "name": f"Fictional Player {i + 1:02d}", "jersey_number": i + 1, "sport": SPORTS[i % len(SPORTS)], "team_id": TEAM_ID} for i in range(count)]


def device_ids(athlete: dict) -> dict[str, str]:
    n = athlete["id"].rsplit("-", 1)[-1]
    return {"physiological": f"fictional-wearable-{n}", "tracking": f"fictional-tracker-{n}"}


def demo_samples(i: int, second: int, duration_s: float, timestamp: datetime) -> dict[str, dict]:
    """Both samples for athlete index i at a given second: the simulator's sine profile, phase-shifted per athlete."""
    span = max(float(duration_s), 1.0)
    elapsed = (second + 7 * i) % span
    hr = heart_rate_at(elapsed, span)
    speed, x_position, acceleration = motion_at(elapsed, span)
    athlete = {"id": f"demo-athlete-{i + 1:02d}"}
    devs = device_ids(athlete)
    ts = timestamp.isoformat()
    return {
        "physiological": {"athlete_id": athlete["id"], "timestamp": ts, "source": "simulated", "device_id": devs["physiological"], "heart_rate_bpm": round(hr, 2)},
        "tracking": {"athlete_id": athlete["id"], "timestamp": ts, "source": "simulated", "device_id": devs["tracking"], "x_position": round(x_position, 3), "y_position": float(i), "speed_mps": round(speed, 3), "acceleration_mps2": round(acceleration, 3), "tracking_confidence": 1.0},
    }


def demo_status(i: int, second: int, timestamp: datetime) -> dict:
    """A clearly fictional device-status report for the athlete's wearable id: 'connected' is this script's claim, not an observation."""
    return {"device_id": f"fictional-wearable-{i + 1:02d}", "timestamp": timestamp.isoformat(), "connected": True, "battery_percent": max(0.0, 100.0 - second * 0.2), "signal_quality": 0.9}


# ---- ports and the served frontend copy ------------------------------------------------------------------------

def port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def choose_port(requested: int | None, what: str) -> int:
    """A free port: the requested one if it is free, otherwise an error (an occupied port is never taken over)."""
    if requested is None:
        return free_port()
    if not port_available(requested):
        raise RuntimeError(f"{what} port {requested} is already in use; choose another or omit it to pick a free port")
    return requested


def prepare_frontend_copy(frontend: Path, destination: Path, backend_url: str) -> Path:
    """Copy the frontend folder (without .git) and point the copy's index.html at the demo backend. The source is untouched."""
    index = frontend / "index.html"
    if not index.is_file():
        raise FileNotFoundError(f"no index.html in {frontend}; pass the Athletrix_Web folder with --frontend")
    shutil.copytree(frontend, destination, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    copy = destination / "index.html"
    text = copy.read_text(encoding="utf-8")
    if API_BASE_PLACEHOLDER not in text:
        raise RuntimeError(f"{index} does not contain the default apiBase {API_BASE_PLACEHOLDER}; cannot point the copy at the demo backend")
    copy.write_text(text.replace(API_BASE_PLACEHOLDER, f'"{backend_url}"'), encoding="utf-8")
    return copy


class StaticServer:
    """python -m http.server for the frontend copy, bound to 127.0.0.1 on the chosen port; stops exactly that process."""

    def __init__(self, directory: Path, port: int) -> None:
        self.directory = directory
        self.port = port
        self.url = f"http://localhost:{port}"
        self.proc: subprocess.Popen | None = None

    def start(self, ready_timeout_s: float = 10.0) -> None:
        self.proc = subprocess.Popen([sys.executable, "-m", "http.server", str(self.port), "--bind", "127.0.0.1", "--directory", str(self.directory)],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        deadline = time.monotonic() + ready_timeout_s
        with httpx.Client(timeout=1.0) as c:
            while time.monotonic() < deadline:
                if self.proc.poll() is not None:
                    raise RuntimeError(f"frontend server exited early with code {self.proc.returncode}")
                try:
                    if c.get(f"http://127.0.0.1:{self.port}/index.html").status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
        self.stop()
        raise RuntimeError(f"frontend server on port {self.port} did not become ready within {ready_timeout_s} s")

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


# ---- feeding the demo team ----------------------------------------------------------------------------------------

@dataclass
class FeedStats:
    seconds: int = 0
    accepted: dict = field(default_factory=lambda: {"physiological": 0, "tracking": 0, "status": 0})
    failed: dict = field(default_factory=lambda: {"physiological": 0, "tracking": 0, "status": 0})


def feed_team(
    base_url: str,
    roster: list[dict],
    duration_s: int,
    *,
    with_status: bool,
    rate_hz: float = DEFAULT_RATE_HZ,
    post: Callable[[str, dict], bool] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    out: Callable[[str], None] = say,
) -> FeedStats:
    """Send one physiological and one tracking sample per athlete per tick for `duration_s` seconds (and a status every 5 s).

    Timestamps are the current time, so the dashboard shows the data live while it is generated; afterwards the readings
    simply age. Failures are counted and reported, never retried, and never replaced by invented successes.
    """
    stats = FeedStats()
    client = None
    if post is None:
        client = httpx.Client(timeout=5.0)

        def post(path: str, body: dict) -> bool:  # noqa: F811
            try:
                return client.post(f"{base_url}{path}", json=body).status_code == 200
            except httpx.HTTPError:
                return False
    ticks = max(1, int(round(duration_s * rate_hz)))
    start = clock()
    try:
        for tick in range(ticks):
            wait = start + tick / rate_hz - clock()
            if wait > 0:
                sleep(wait)
            stamp = now()
            second = int(tick / rate_hz)
            for i, athlete in enumerate(roster):
                samples = demo_samples(i, second, duration_s, stamp)
                for stream in ("physiological", "tracking"):
                    ok = post(f"/api/v1/samples/{stream}", samples[stream])
                    stats.accepted[stream] += int(ok)
                    stats.failed[stream] += int(not ok)
                if with_status and tick % int(max(1, STATUS_EVERY_S * rate_hz)) == 0:
                    ok = post("/api/v1/devices/status", demo_status(i, second, stamp))
                    stats.accepted["status"] += int(ok)
                    stats.failed["status"] += int(not ok)
            stats.seconds = second + 1
            if rate_hz == 1.0 and tick % 10 == 9:
                out(f"  … {tick + 1} s of {duration_s} s of fictional data sent ({stats.accepted['physiological']} physiological, {stats.accepted['tracking']} tracking accepted, {sum(stats.failed.values())} failed)")
    finally:
        if client is not None:
            client.close()
    return stats


# ---- the launcher --------------------------------------------------------------------------------------------------

class Demo:
    """Owns the temporary folder and the two child processes; cleanup stops only those."""

    def __init__(self, frontend: Path, backend_port: int | None, frontend_port: int | None, retention_caps: dict[str, int] | None = None, extra_env: dict[str, str] | None = None) -> None:
        self.frontend = frontend
        self.tmp = tempfile.TemporaryDirectory(prefix="athletrix-demo-")
        self.tmp_path = Path(self.tmp.name)
        self.backend_port = choose_port(backend_port, "backend")
        self.frontend_port = choose_port(frontend_port, "frontend")
        self.retention_caps = dict(retention_caps or {})
        self.backend = IsolatedBackend(port=self.backend_port, cors_origins=[f"http://localhost:{self.frontend_port}", f"http://127.0.0.1:{self.frontend_port}"], retention_caps=self.retention_caps,
                                      **({"extra_env": extra_env} if extra_env else {}))
        self.before_start: Callable[[Path], None] | None = None   # writes into the empty database before the backend opens it
        self.static: StaticServer | None = None
        self.cleaned = False

    @property
    def backend_url(self) -> str:
        return f"http://127.0.0.1:{self.backend_port}"

    @property
    def dashboard_url(self) -> str:
        return f"http://localhost:{self.frontend_port}/#demo"

    def start(self) -> None:
        if self.before_start is not None:
            self.before_start(self.backend.db_path)
        self.backend.start()
        copy_dir = self.tmp_path / "web"
        prepare_frontend_copy(self.frontend, copy_dir, self.backend_url)
        self.static = StaticServer(copy_dir, self.frontend_port)
        self.static.start()

    def cleanup(self) -> None:
        if self.cleaned:
            return
        self.cleaned = True
        if self.static is not None:
            self.static.stop()
        self.backend.cleanup()   # stops the backend process and removes its own temporary database folder
        self.tmp.cleanup()


def register_demo_roster(base_url: str, roster: list[dict], client: httpx.Client | None = None) -> tuple[int, int]:
    own = client is None
    client = client or httpx.Client(timeout=5.0)
    registered = failed = 0
    try:
        for athlete in roster:
            r = client.post(f"{base_url}/api/v1/athletes", json=athlete)
            if r.status_code in (201, 409):
                registered += 1
            else:
                failed += 1
    finally:
        if own:
            client.close()
    return registered, failed


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--frontend", type=Path, default=DEFAULT_FRONTEND, help=f"folder containing the Athletrix_Web index.html (default {DEFAULT_FRONTEND})")
    p.add_argument("--athletes", type=int, default=DEFAULT_ATHLETES, help=f"fictional athletes on the demo team (default {DEFAULT_ATHLETES})")
    p.add_argument("--duration", type=int, default=None, help=f"seconds of fictional data to generate (default {DEFAULT_DURATION_S}; with --session match: scenario seconds, default 360)")
    p.add_argument("--session", choices=["simple", "match", "pilot"], default="pilot", help="pilot: the FICTIONAL fatigue workflow with the dashboard (default); simple: the sine-wave feed; match: the paced fictional match session (legacy; its review features are no longer shown) (scripts/live_session.py)")
    p.add_argument("--rounds", type=int, default=3, help="--session pilot: rounds per participant (default 3)")
    p.add_argument("--keep-output", type=Path, help="--session pilot: copy the rehearsal summary, export and report to this new folder before cleaning up")
    p.add_argument("--speed", type=float, default=1.0, help="--session match: scenario seconds per wall second (default 1; timestamps stay real send times)")
    p.add_argument("--device-status", action="store_true", help="also send clearly fictional device-status reports every 5 s")
    p.add_argument("--backend-port", type=int, default=None, help="backend port (default: a free port; an occupied port is an error)")
    p.add_argument("--frontend-port", type=int, default=None, help="frontend port (default: a free port; an occupied port is an error)")
    p.add_argument("--exit-after-demo", action="store_true", help="stop the servers when data generation ends instead of waiting for Ctrl-C")
    p.add_argument("--physiological-cap", type=int, default=None, help="newest physiological samples the demo backend keeps per athlete (default: the backend's default, 300); its own environment only, .env is untouched")
    p.add_argument("--tracking-cap", type=int, default=None, help="newest tracking samples the demo backend keeps per athlete (default: the backend's default, 300); a count, not a time span")
    return p


def main(argv: list[str] | None = None, *, out: Callable[[str], None] = say, wait_for_interrupt: Callable[[], None] | None = None, demo_factory: Callable = None) -> int:
    args = build_parser().parse_args(argv)
    if args.duration is None:
        args.duration = 360 if args.session == "match" else DEFAULT_DURATION_S
    if args.session == "match":
        return run_match(args, out, wait_for_interrupt, demo_factory)
    if args.session == "pilot":
        return run_pilot(args, out, wait_for_interrupt, demo_factory)
    if args.athletes < 1 or args.duration < 1:
        out("athletes and duration must be at least 1")
        return 2
    caps = {stream: cap for stream, cap in (("physiological", args.physiological_cap), ("tracking", args.tracking_cap)) if cap is not None}
    if any(cap < 1 for cap in caps.values()):
        out("retention caps must be positive integers")
        return 2
    frontend = args.frontend.expanduser()
    if not (frontend / "index.html").is_file():
        out(f"Frontend folder not found or has no index.html: {frontend}. Pass the Athletrix_Web folder with --frontend.")
        return 2
    try:
        demo = (demo_factory or Demo)(frontend, args.backend_port, args.frontend_port, caps)
    except RuntimeError as exc:
        out(f"Cannot start the demo: {exc}")
        return 1
    stopping = {"done": False}

    def on_signal(signum, _frame):
        if not stopping["done"]:
            stopping["done"] = True
            out("\nStopping the demo (only the backend and frontend copies started here are stopped; the temporary folder is removed)…")
            demo.cleanup()
        raise SystemExit(130)

    previous = {s: signal.signal(s, on_signal) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        out("ATHLETRIX ISOLATED DEMO · fictional athletes and simulated data only · nothing is uploaded anywhere")
        out(f"Temporary folder: {demo.tmp_path} (removed on exit) · demo database: {demo.backend.db_path}")
        if caps:
            out("Retention caps for this demo backend only (its environment, not .env): " + ", ".join(f"{k} {v} samples per athlete" for k, v in caps.items()) + " · a count, not a time span")
        try:
            demo.start()
        except (RuntimeError, FileNotFoundError, OSError) as exc:
            out(f"Startup failed: {exc}")
            return 1
        roster = demo_roster(args.athletes)
        registered, failed = register_demo_roster(demo.backend_url, roster)
        out(f"Backend: {demo.backend_url} (pid {demo.backend.proc.pid if demo.backend.proc else '?'}) · frontend copy: {demo.static.url if demo.static else '?'}")
        out(f"Registered {registered} fictional athletes on team {TEAM_ID} ({failed} failed): " + ", ".join(f"{a['name']} ({a['sport']})" for a in roster))
        out("")
        out(f"OPEN THE DASHBOARD:  {demo.dashboard_url}")
        out("  Click Live Demo. The Live team overview and the athlete cards show the fictional readings as they arrive (1 Hz per stream).")
        out("  Switch Review to Recorded to review retained samples, pick an effort from the timeline, compare two efforts, or download the retained samples.")
        out(f"  Fictional data is generated for {args.duration} s; after that the readings age and show as 'stopped updating', and Recorded mode still works.")
        out("  Press Ctrl-C to stop the demo; the temporary database and frontend copy are deleted." if not args.exit_after_demo else "  The demo stops by itself when data generation ends.")
        out("")
        stats = feed_team(demo.backend_url, roster, args.duration, with_status=args.device_status, out=out)
        out(f"Data generation finished at {datetime.now(UTC).isoformat(timespec='seconds')}: {stats.accepted['physiological']} physiological, {stats.accepted['tracking']} tracking"
            + (f", {stats.accepted['status']} device-status reports" if args.device_status else "") + f" accepted; {sum(stats.failed.values())} failed. Nothing more will be sent; the dashboard keeps the retained readings.")
        if args.exit_after_demo:
            return 0 if sum(stats.failed.values()) == 0 else 1
        out("Servers stay up for exploring the dashboard. Press Ctrl-C to stop and clean up.")
        (wait_for_interrupt or (lambda: signal.pause()))()
        return 0
    finally:
        if not stopping["done"]:
            stopping["done"] = True
            demo.cleanup()
        for s, h in previous.items():
            signal.signal(s, h)


def run_pilot(args, out, wait_for_interrupt, demo_factory) -> int:
    """--session pilot: the FICTIONAL fatigue-pilot rehearsal (scripts/pilot_rehearsal.py) against an isolated backend
    with a FICTIONAL profile, the dashboard copy pointed at it, an engine restart after the first participant, and the
    export, fresh-database reproduction and paired report at the end."""
    import types  # noqa: PLC0415

    import pilot_rehearsal as pr  # noqa: PLC0415
    import live_session  # noqa: PLC0415
    frontend = args.frontend.expanduser()
    if not (frontend / "index.html").is_file():
        out(f"Frontend folder not found or has no index.html: {frontend}. Pass the Athletrix_Web folder with --frontend.")
        return 2
    try:
        demo = (demo_factory or Demo)(frontend, args.backend_port, args.frontend_port, {}, {"FATIGUE_PROFILE_PATH": str(pr.PROFILE)})
    except RuntimeError as exc:
        out(f"Cannot start the rehearsal: {exc}")
        return 1
    prepared = {}
    players = [(aid, f"Fictional participant {code}", int(aid[2:]) + 10, False, None, [], True) for code, aid, _, _ in pr.PARTICIPANTS]

    def before(db_path):
        prepared["rest"], _ = live_session.prepare_database(db_path, datetime.now(UTC), players=players)
    demo.before_start = before

    def on_signal(signum, _frame):
        out("\nStopping (only the backend and frontend copy started here are stopped; the temporary folder is removed)…")
        demo.cleanup()
        raise SystemExit(130)
    previous = {sg: signal.signal(sg, on_signal) for sg in (signal.SIGINT, signal.SIGTERM)}
    out_dir = demo.tmp_path / "pilot-output"
    out_dir.mkdir()
    try:
        out("ATHLETRIX FICTIONAL FATIGUE-PILOT REHEARSAL · made-up participants, readings and sprint times · no camera, wearable or person involved")
        try:
            demo.start()
        except (RuntimeError, FileNotFoundError, OSError) as exc:
            out(f"Startup failed: {exc}")
            return 1
        out(f"OPEN THE DASHBOARD:  {demo.dashboard_url}  (click Live Demo; the Fatigue pilot card is at the top)")
        out("  The script plays the operator: it assigns the wearer, saves each score, confirms it, starts the sprint, enters times on the")
        out("  blind timing sheet, misses one save (P2 round 1), marks one round invalid (P1 round 3), drops the strap (P2 round 2),")
        out("  loses the camera before a save (P3 round 2) and restarts the engine after P1. You can press Save score yourself between rounds.")
        api = pr.Api(demo.backend_url, httpx.Client(timeout=15.0))
        ns = types.SimpleNamespace(mode="wall", rounds=args.rounds, round_s=70.0, sprint_s=4.0, rest_s=5.0, dropout_s=20.0, duration_min=0.0, profile=pr.PROFILE, study_manifest=pr.STUDY_MANIFEST,
                                   rest_id=prepared["rest"], out=out_dir)

        def restart():
            demo.backend.proc.terminate(); demo.backend.proc.wait(timeout=10); demo.backend.proc = None
            demo.backend.start()
        log = pr.run(ns, api, lambda: datetime.now(UTC), time.sleep, out, restart)
        fin = pr.finish(ns, api, log, out)
        summary = {"mode": "wall (launcher --session pilot)", "fictional": True, "log": {k: v for k, v in log.items() if k != "events"}, "events": log.get("events"), **fin,
                   "failed_requests": api.failed, "requests": api.sent,
                   "request_latency_ms": {k: {"p50": round(1000 * sorted(v)[len(v) // 2], 2), "p95": round(1000 * sorted(v)[int(0.95 * len(v))], 2), "max": round(1000 * max(v), 2)}
                                          for k, v in api.lat.items() if v}}
        import json as _json  # noqa: PLC0415
        (out_dir / "summary.json").write_text(_json.dumps(summary, indent=2, default=str))
        if args.keep_output:
            shutil.copytree(out_dir, args.keep_output)
            out(f"Copied the summary, export and report to {args.keep_output}")
        out(f"Done. Saved predictions reproduced in a fresh database: {fin['reproduction_on_second_database']['all_reproduced']}; "
            f"report: {out_dir / 'report' / 'report.md'}" + ("" if args.keep_output else " (the temporary folder is removed on exit: use --keep-output)"))
        out(f"Failed requests: {api.failed or 'none'}; pilot view API latency (script polling once a second): {summary['request_latency_ms'].get('pilot_view')}")
        if args.exit_after_demo:
            return 0 if fin["reproduction_on_second_database"]["all_reproduced"] else 1
        out("Servers stay up for exploring the dashboard. Press Ctrl-C to stop and clean up.")
        (wait_for_interrupt or (lambda: signal.pause()))()
        return 0
    finally:
        for sg, h in previous.items():
            signal.signal(sg, h)
        demo.cleanup()


def run_match(args, out, wait_for_interrupt, demo_factory) -> int:
    """--session match: the paced fictional match session against an isolated backend with the FICTIONAL live profiles."""
    import live_session  # noqa: PLC0415
    if args.duration < 60 or not (0.1 <= args.speed <= 20):
        out("--duration must be at least 60 scenario seconds and --speed between 0.1 and 20")
        return 2
    frontend = args.frontend.expanduser()
    if not (frontend / "index.html").is_file():
        out(f"Frontend folder not found or has no index.html: {frontend}. Pass the Athletrix_Web folder with --frontend.")
        return 2
    env = {k: str(v) for k, v in live_session.LIVE_PROFILES.items()}
    try:
        demo = (demo_factory or Demo)(frontend, args.backend_port, args.frontend_port, {}, env)
    except RuntimeError as exc:
        out(f"Cannot start the demo: {exc}")
        return 1
    prepared = {}

    def before(db_path):
        prepared["rest"], prepared["bases"] = live_session.prepare_database(db_path, datetime.now(UTC))
    demo.before_start = before
    stopping = {"done": False}

    def on_signal(signum, _frame):
        if not stopping["done"]:
            stopping["done"] = True
            out("\nStopping the session (only the backend and frontend copy started here are stopped; the temporary folder is removed)…")
            demo.cleanup()
        raise SystemExit(130)

    previous = {s: signal.signal(s, on_signal) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        out("ATHLETRIX FICTIONAL MATCH SESSION · made-up athletes, data, profiles and coach decisions · a paced script, NOT live capture")
        out(f"Temporary folder: {demo.tmp_path} (removed on exit)")
        try:
            demo.start()
        except (RuntimeError, FileNotFoundError, OSError) as exc:
            out(f"Startup failed: {exc}")
            return 1
        session = live_session.Session(demo.backend_url, prepared["rest"], prepared["bases"], duration_s=args.duration, speed=args.speed, out=out)
        try:
            rid = session.setup()
        except RuntimeError as exc:
            out(f"Session setup failed: {exc}")
            return 1
        wall = args.duration / args.speed
        out(f"Backend {demo.backend_url} (FICTIONAL live profiles in its environment only) · frontend copy {demo.static.url if demo.static else '?'} · recording {rid}")
        out("")
        out(f"OPEN THE DASHBOARD:  {demo.dashboard_url}")
        out("  1. Click Live Demo. The Team review card lists every player (roster order, never ranked) with status, latest reading,")
        out("     experimental index, performance score, evidence age and the current experimental output. Click a player for details.")
        out("  2. Watch the phases: p08's index rises then recovers; p02's performance declines; at half time the formation changes and")
        out("     p05 is asked to play left wing-back; p06's strap stops; b01 becomes unavailable; simulated coach responses are recorded.")
        out("  3. Record your own response or a lineup change from the player's details (it is stored as a person's response).")
        out(f"  The script runs {args.duration:g} scenario seconds at speed {args.speed:g} (about {wall / 60:.1f} min). Ctrl-C stops and cleans up.")
        out("")
        stats = session.run()
        try:
            session.client.post(f"{demo.backend_url}/api/v1/recordings/{rid}/stop")
        except Exception:  # noqa: BLE001
            pass
        out("Script finished; the session recording was stopped (open it with Review). Events:")
        for e in stats.events:
            out(f"  - {e}")
        out(f"Sent: {stats.sent}; failed: {stats.failed or 'none'}")
        if args.exit_after_demo:
            return 0 if not stats.failed else 1
        out("Servers stay up for exploring the dashboard. Press Ctrl-C to stop and clean up.")
        (wait_for_interrupt or (lambda: signal.pause()))()
        return 0
    finally:
        if not stopping["done"]:
            stopping["done"] = True
            demo.cleanup()
        for s, h in previous.items():
            signal.signal(s, h)


if __name__ == "__main__":
    sys.exit(main())
