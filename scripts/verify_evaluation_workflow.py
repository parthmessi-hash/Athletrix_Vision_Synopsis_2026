"""One reproducible evaluation of the start-time configuration snapshot, timing metadata, package round trip and local
removal, on isolated engines with fictional data only.

WHAT IT DOES (normal development servers, databases and recordings are never touched; everything lives in a temporary
folder and on free localhost ports, and is removed at the end)
  1. Source engine (CODE_REVISION and a 4.0 m/s high-speed threshold supplied at startup, rolling caps 20/20). Three
     fictional athletes: eval-01 and eval-02 share recording R1 (started with an operator-supplied adapter note);
     eval-03 has its own recording R2 and rolling-only samples. Samples carry past timestamps (membership is decided at
     arrival, not by timestamp): missing heart rates, real zero speeds, an equal-instant replacement and one delayed
     arrival.
  2. Checks the snapshot captured at start, the documented null/zero/duplicate/delayed behaviour, and what the timing
     metadata means: every receipt time lies between this script's own clock readings just before and after the POST
     (same machine, so the bound is meaningful here), never equals the source timestamp, follows arrival order rather
     than timestamp order, and the replacement's receipt replaces the earlier one; Server-Timing never exceeds the
     client's own round trip (both monotonic clocks on one machine).
  3. Stops R1, saves periods, restarts the source with a 6.0 m/s threshold: the start-time snapshot is unchanged while
     the export-time settings and reviews follow the new value. Exports the package, inspects it, dry-runs and imports
     it into a destination engine: the snapshot and receipts arrive with origin "source_engine", samples and reviews
     are identical.
  4. Removal (engines stopped): a dry run changes no data; removing eval-01 in the destination leaves eval-02's
     recorded samples and reviews identical; a forced failure part-way rolls back completely; removing R1 in the
     source keeps R2 and all rolling data; a running engine blocks execution.
  5. Older formats: a version 1 package (no snapshot, no receipts) and a database with the tables as written before
     this batch both open, report the snapshot as unavailable and receipts as absent, export, import and clean up.
  Writes a short JSON report (reports/ is ignored by git): expected versus actual per check, failures, unknowns and
  limitations. Exit 0 when every check passed.

WHAT IT IS NOT
  A software check with fictional data on one machine. It does not validate physiology, devices, real network timing
  or the dashboard's display timing (see the developer timing panel), and removal here is logical deletion only.

USAGE
    python scripts/verify_evaluation_workflow.py [--report reports/evaluation-workflow.json]
"""

import argparse
import hashlib
import io
import json
import signal
import sqlite3
import sys
import tempfile
import time
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

import local_data  # noqa: E402
import recording_package  # noqa: E402
from validate_team_scale import IsolatedBackend  # noqa: E402

A1, A2, A3 = "eval-01", "eval-02", "eval-03"
SECONDS = 40
NULL_EVERY, ZERO_EVERY = 10, 7          # heart rate null when s % 10 == 0; speed 0.0 when s % 7 == 0
REPLACED_S, DELAYED_S = 20, 5           # eval-01 second 20 is sent twice; eval-02 second 5 arrives after second 39
ADAPTER = {"note": "fictional wearable bridge v0.1, 1 Hz; fictional camera 1 Hz", "metadata": {"wearable_rate_hz": 1, "camera_rate_hz": 1, "calibrated": False}}

# Tables as written by the engine before this batch (backend 1bfd555): no receipt columns, no recording_configurations.
OLDER_SCHEMA = """
CREATE TABLE athletes (id TEXT PRIMARY KEY, name TEXT NOT NULL, jersey_number INTEGER NOT NULL, sport TEXT NOT NULL, team_id TEXT NOT NULL);
CREATE TABLE device_status (device_id TEXT PRIMARY KEY, ts_utc TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE physiological_samples (athlete_id TEXT NOT NULL, ts_utc TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY (athlete_id, ts_utc));
CREATE TABLE tracking_samples (athlete_id TEXT NOT NULL, ts_utc TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY (athlete_id, ts_utc));
CREATE TABLE recordings (id TEXT PRIMARY KEY, name TEXT NOT NULL, started_at TEXT NOT NULL, stopped_at TEXT);
CREATE UNIQUE INDEX recordings_one_active ON recordings (stopped_at IS NULL) WHERE stopped_at IS NULL;
CREATE TABLE recording_athletes (recording_id TEXT NOT NULL, athlete_id TEXT NOT NULL, name TEXT NOT NULL, jersey_number INTEGER NOT NULL, sport TEXT NOT NULL, team_id TEXT NOT NULL, PRIMARY KEY (recording_id, athlete_id));
CREATE TABLE recording_physiological_samples (recording_id TEXT NOT NULL, athlete_id TEXT NOT NULL, ts_utc TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY (recording_id, athlete_id, ts_utc));
CREATE TABLE recording_tracking_samples (recording_id TEXT NOT NULL, athlete_id TEXT NOT NULL, ts_utc TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY (recording_id, athlete_id, ts_utc));
CREATE TABLE recording_periods (id TEXT PRIMARY KEY, recording_id TEXT NOT NULL, athlete_id TEXT NOT NULL, label TEXT NOT NULL, effort_start TEXT NOT NULL, effort_end TEXT NOT NULL, start_utc TEXT NOT NULL, end_utc TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE recording_imports (recording_id TEXT PRIMARY KEY, source_recording_id TEXT NOT NULL, package_sha256 TEXT NOT NULL, package_format TEXT NOT NULL, package_generated_at TEXT NOT NULL, imported_at TEXT NOT NULL, period_id_map TEXT NOT NULL);
"""


class Backend(IsolatedBackend):
    """An isolated backend with extra settings for this run only (passed as environment variables; .env is untouched)."""

    def __init__(self, extra: dict[str, str] | None = None, **kw) -> None:
        super().__init__(**kw)
        self.extra = dict(extra or {})

    def environment(self) -> dict[str, str]:
        return {**super().environment(), **self.extra}


def z(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def parse(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path: Path) -> list[str]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return sorted(con.iterdump())
    finally:
        con.close()


def query(path: Path, sql: str, *params) -> list:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def strip(review: dict) -> dict:
    return {k: v for k, v in review.items() if k not in ("captured_at", "recording")}


def cli(main: Callable, *argv, **kw) -> tuple[int, str]:
    lines: list[str] = []
    code = main([str(a) for a in argv], out=lines.append, **kw)
    return code, "\n".join(lines)


def to_v1(package: bytes) -> bytes:
    """The same recording as a format version 1 package: no configuration snapshot, no receipts member."""
    with zipfile.ZipFile(io.BytesIO(package)) as zf:
        files = {n: zf.read(n) for n in zf.namelist()}
    rec = {k: v for k, v in json.loads(files["recording.json"]).items() if k != "configuration_at_start"}
    files["recording.json"] = json.dumps(rec, indent=2).encode()
    manifest = json.loads(files.pop("manifest.json"))
    files.pop("receipts.jsonl")
    manifest["format_version"] = 1
    manifest["counts"].pop("receipts")
    manifest["members"] = {n: {"sha256": hashlib.sha256(b).hexdigest(), "bytes": len(b)} for n, b in files.items()}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        for n, b in files.items():
            zf.writestr(n, b)
    return buf.getvalue()


class Checks:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def __call__(self, name: str, expected, actual, ok: bool | None = None) -> bool:
        passed = (expected == actual) if ok is None else bool(ok)
        self.items.append({"check": name, "expected": expected, "actual": actual, "passed": passed})
        return passed


def run(out: Callable[[str], None], workdir: Path, backend_factory: Callable = Backend) -> dict:
    check = Checks()
    unknowns: list[str] = []
    t0 = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=600)   # source timestamps in the past (historical replay style)
    at = lambda s: t0 + timedelta(seconds=s)  # noqa: E731
    src_env = {"CODE_REVISION": "eval-fictional", "HIGH_SPEED_THRESHOLD_MPS": "4.0"}
    source = backend_factory(extra=src_env, retention_caps={"physiological": 20, "tracking": 20})
    dest = backend_factory(extra={"HIGH_SPEED_THRESHOLD_MPS": "6.0"})
    try:
        source.start()
        b = source.base_url
        posts: dict[tuple, dict] = {}       # (athlete, stream, second) -> {"before", "after", "rtt_ms", "server_ms"}
        with httpx.Client(timeout=30) as c:
            def post(stream, body, key):
                before = datetime.now(UTC); p0 = time.perf_counter()
                r = c.post(f"{b}/api/v1/samples/{stream}", json=body)
                rtt = (time.perf_counter() - p0) * 1000; after = datetime.now(UTC)
                assert r.status_code == 200, r.text
                server = float(r.headers["server-timing"].split("dur=")[1].split(";")[0])
                posts[key] = {"before": before, "after": after, "rtt_ms": rtt, "server_ms": server}

            def phys(a, s, hr):
                return {"athlete_id": a, "timestamp": z(at(s)), "source": "simulated", "device_id": f"w-{a}", "heart_rate_bpm": hr}

            def trk(a, s, speed):
                return {"athlete_id": a, "timestamp": z(at(s)), "x_position": float(s), "y_position": 0.0, "speed_mps": speed, "acceleration_mps2": 0.0, "tracking_confidence": 1.0, "device_id": "cam-1"}

            for n, a in enumerate((A1, A2, A3), start=1):
                assert c.post(f"{b}/api/v1/athletes", json={"id": a, "name": f"Fictional Evaluation {n:02d}", "jersey_number": n, "sport": "fictional", "team_id": "eval"}).status_code == 201
            for s in range(5):                                   # eval-03: rolling only, before any recording
                post("physiological", phys(A3, -100 + s, 90.0), (A3, "physiological", -100 + s))
            r1 = c.post(f"{b}/api/v1/recordings", json={"name": "evaluation shared", "athlete_ids": [A1, A2], "adapter_configuration": ADAPTER}).json()["id"]
            for s in range(SECONDS):
                for a in (A1, A2):
                    if a == A2 and s == DELAYED_S:
                        continue
                    post("physiological", phys(a, s, None if s % NULL_EVERY == 0 else 100.0 + s), (a, "physiological", s))
                    post("tracking", trk(a, s, 0.0 if s % ZERO_EVERY == 0 else 3.0 + (s % 4)), (a, "tracking", s))
            post("physiological", phys(A1, REPLACED_S, 150.0), (A1, "physiological", "replacement"))
            post("physiological", phys(A2, DELAYED_S, 111.0), (A2, "physiological", DELAYED_S))
            post("tracking", trk(A2, DELAYED_S, 4.0), (A2, "tracking", DELAYED_S))
            live = c.get(f"{b}/api/v1/athletes/{A2}/live").json()
            c.post(f"{b}/api/v1/recordings/{r1}/stop")
            r2 = c.post(f"{b}/api/v1/recordings", json={"name": "evaluation solo", "athlete_ids": [A3]}).json()["id"]
            for s in range(10):
                post("physiological", phys(A3, 100 + s, 95.0), (A3, "physiological", 100 + s))
            c.post(f"{b}/api/v1/recordings/{r2}/stop")
            for a, (lo, hi) in ((A1, (5, 15)), (A1, (18, 30)), (A2, (2, 12))):
                assert c.post(f"{b}/api/v1/recordings/{r1}/athletes/{a}/periods", json={"label": f"{a} effort", "effort_start": z(at(lo)), "effort_end": z(at(hi))}).status_code == 201

            detail = c.get(f"{b}/api/v1/recordings/{r1}").json()
            cfg = detail["configuration_at_start"]
            snap = cfg["snapshot"] or {}
            check("snapshot captured at start by this engine", ["captured", "this_engine"], [cfg["status"], cfg["origin"]])
            check("snapshot time equals the recording start", detail["started_at"], snap.get("captured_at"))
            check("snapshot settings (threshold, caps, revision)", [4.0, 20, 20, "eval-fictional"],
                  [snap["movement"]["high_speed_threshold_mps"], snap["retention"]["physiological_retention_samples"], snap["retention"]["tracking_retention_samples"], snap["engine"]["code_revision"]])
            check("operator-supplied adapter note stored as given, marked unverified", {**ADAPTER, "verified_by_engine": False}, snap.get("operator_supplied"))

            rows = {a: [json.loads(x) for x in c.get(f"{b}/api/v1/recordings/{r1}/athletes/{a}/recorded-samples.jsonl").text.splitlines()] for a in (A1, A2)}
            p1 = [x["sample"] for x in rows[A1] if x["type"] == "physiological"]
            t1 = [x["sample"] for x in rows[A1] if x["type"] == "tracking"]
            check("recorded counts per stream (eval-01, eval-02; beyond the rolling cap of 20)", [SECONDS] * 4,
                  [len(p1), len(t1)] + [sum(1 for x in rows[A2] if x["type"] == t) for t in ("physiological", "tracking")])
            nulls = [s for s in range(SECONDS) if s % NULL_EVERY == 0 and s != REPLACED_S]
            check("missing heart rates stay null, never zero", len(nulls), sum(1 for x in p1 if x["heart_rate_bpm"] is None))
            check("zero speeds stay 0.0", sum(1 for s in range(SECONDS) if s % ZERO_EVERY == 0), sum(1 for x in t1 if x["speed_mps"] == 0.0))
            check("equal-instant replacement keeps one row with the later values", [1, 150.0], [sum(1 for x in p1 if x["timestamp"] == z(at(REPLACED_S))), next(x["heart_rate_bpm"] for x in p1 if x["timestamp"] == z(at(REPLACED_S)))])
            check("delayed arrival is recorded at its own timestamp", 111.0,
                  next((x["sample"]["heart_rate_bpm"] for x in rows[A2] if x["type"] == "physiological" and x["sample"]["timestamp"] == z(at(DELAYED_S))), None))
            check("delayed arrival does not become the current reading", z(at(SECONDS - 1)), live["physiological"]["timestamp"])
            check("receipt counts equal recorded counts", [SECONDS] * 4, [e[s]["receipt_count"] for e in detail["recorded"] for s in ("physiological", "tracking")])

            source.stop()
            source.extra["HIGH_SPEED_THRESHOLD_MPS"] = "6.0"       # restart with another setting
            source.start()
            after_restart = c.get(f"{b}/api/v1/recordings/{r1}").json()["configuration_at_start"]
            check("snapshot unchanged after restart with a changed setting", cfg, after_restart)
            batch = {a: c.get(f"{b}/api/v1/recordings/{r1}/athletes/{a}/period-reviews").json() for a in (A1, A2)}
            check("reviews apply the engine's current settings, not the snapshot", 6.0, batch[A1]["distance_rules"]["high_speed_threshold_mps"])
            package = c.get(f"{b}/api/v1/recordings/{r1}/package.zip").content
            r2_jsonl = c.get(f"{b}/api/v1/recordings/{r2}/athletes/{A3}/recorded-samples.jsonl").text
        source.stop()

        with zipfile.ZipFile(io.BytesIO(package)) as zf:
            manifest = json.loads(zf.read("manifest.json"))
            rec_doc = json.loads(zf.read("recording.json"))
            receipts = {(r["athlete_id"], r["type"], r["timestamp"]): r["received_at"] for r in map(json.loads, zf.read("receipts.jsonl").decode().splitlines())}
        check("package keeps the start snapshot apart from export-time settings", [4.0, 6.0, 2],
              [rec_doc["configuration_at_start"]["movement"]["high_speed_threshold_mps"], manifest["settings_at_export"]["high_speed_threshold_mps"], manifest["format_version"]])
        bounded = all(posts[k]["before"] <= parse(receipts[(k[0], k[1], z(at(k[2])))]) <= posts[k]["after"] for k in posts if k[0] in (A1, A2) and k[2] != "replacement" and k != (A1, "physiological", REPLACED_S))
        check("every receipt lies between the script's clock readings around its POST (same machine)", True, bounded)
        check("receipt time is not the source timestamp", True, all(parse(v) - parse(k[2]) > timedelta(seconds=60) for k, v in receipts.items()))
        check("receipts follow arrival order: the delayed sample was received after second 39",
              True, parse(receipts[(A2, "physiological", z(at(DELAYED_S)))]) > parse(receipts[(A2, "physiological", z(at(SECONDS - 1)))]))
        rep = posts[(A1, "physiological", "replacement")]
        check("the replacement's receipt replaced the earlier one", True, rep["before"] <= parse(receipts[(A1, "physiological", z(at(REPLACED_S)))]) <= rep["after"])
        check("Server-Timing never exceeds the client's round trip", True, all(v["server_ms"] <= v["rtt_ms"] + 0.5 for v in posts.values()))
        server_ms = sorted(v["server_ms"] for v in posts.values())
        rtts = sorted(v["rtt_ms"] for v in posts.values())

        pkg_path = workdir / "evaluation.zip"
        pkg_path.write_bytes(package)
        code, _ = cli(recording_package.main, "inspect", pkg_path)
        check("package inspects cleanly", 0, code)
        code, _ = cli(recording_package.main, "import", pkg_path, "--database", dest.db_path, "--dry-run")
        check("import dry run leaves the destination missing", [0, False], [code, dest.db_path.exists()])
        code, text = cli(recording_package.main, "import", pkg_path, "--database", dest.db_path, "--json")
        new_r1 = json.loads(text)["recording_id"]
        check("import succeeds", 0, code)
        imported = query(dest.db_path, "SELECT receipt FROM recording_physiological_samples WHERE recording_id = ?", new_r1)
        check("imported receipts are the source engine's, never this engine's", True, bool(imported) and all(json.loads(r[0])["origin"] == "source_engine" for r in imported))
        check("destination rolling history untouched by the import", [0, 0], [query(dest.db_path, "SELECT COUNT(*) FROM physiological_samples")[0][0], query(dest.db_path, "SELECT COUNT(*) FROM tracking_samples")[0][0]])

        dest.start()
        with httpx.Client(timeout=30) as c:
            d = dest.base_url
            dcfg = c.get(f"{d}/api/v1/recordings/{new_r1}").json()["configuration_at_start"]
            check("snapshot arrives with origin source_engine and identical content", ["source_engine", cfg["snapshot"]], [dcfg["origin"], dcfg["snapshot"]])
            same = all(c.get(f"{d}/api/v1/recordings/{new_r1}/athletes/{a}/recorded-samples.jsonl").text.splitlines() == [json.dumps(x, ensure_ascii=False) for x in rows[a]] for a in (A1, A2))
            check("samples identical after the round trip", True, same)
            dbatch = {a: c.get(f"{d}/api/v1/recordings/{new_r1}/athletes/{a}/period-reviews").json() for a in (A1, A2)}
            check("batch reviews identical under the same settings", [[strip(i["review"]) for i in batch[a]["items"]] for a in (A1, A2)], [[strip(i["review"]) for i in dbatch[a]["items"]] for a in (A1, A2)])
            v1 = to_v1(package)
            (workdir / "evaluation-v1.zip").write_bytes(v1)
        dest.stop()
        code, _ = cli(recording_package.main, "import", workdir / "evaluation-v1.zip", "--database", dest.db_path, "--json")
        check("a version 1 package still imports", 0, code)
        v1_id = query(dest.db_path, "SELECT recording_id FROM recording_imports WHERE package_format = 'athletrix-recording-package/1'")[0][0]
        dest.start()
        with httpx.Client(timeout=30) as c:
            v1_detail = c.get(f"{dest.base_url}/api/v1/recordings/{v1_id}").json()
            check("version 1 import: snapshot unavailable, no receipts", ["unavailable", 0], [v1_detail["configuration_at_start"]["status"], sum(e[s]["receipt_count"] for e in v1_detail["recorded"] for s in ("physiological", "tracking"))])
            check("version 1 import: same samples", True, c.get(f"{dest.base_url}/api/v1/recordings/{v1_id}/athletes/{A2}/recorded-samples.jsonl").text.splitlines() == [json.dumps(x, ensure_ascii=False) for x in rows[A2]])
            code, text = cli(local_data.main, "remove-athlete", A1, "--database", dest.db_path, "--execute", "--confirm", A1)
            check("removal refused while the destination engine runs", True, code == 1 and "another connection has it open" in text)
        dest.stop()

        # ---- removal on the stopped destination ----
        before_hash = file_hash(dest.db_path)
        code, text = cli(local_data.main, "remove-athlete", A1, "--database", dest.db_path, "--json")
        plan = json.loads(text)
        check("athlete dry run changes no data", [0, before_hash], [code, file_hash(dest.db_path)])
        before_dump = dump(dest.db_path)
        def boom(stage, calls=[]):  # noqa: B006
            calls.append(stage)
            if len(calls) == 2:
                raise RuntimeError("simulated failure part-way")
        code, text = cli(local_data.main, "remove-athlete", A1, "--database", dest.db_path, "--execute", "--confirm", A1, checkpoint=boom)
        check("failed deletion rolls back completely", [1, True], [code, dump(dest.db_path) == before_dump])
        code, text = cli(local_data.main, "remove-athlete", A1, "--database", dest.db_path, "--execute", "--confirm", A1)
        check("athlete removal executes", 0, code)
        left = sum(query(dest.db_path, f"SELECT COUNT(*) FROM {t} WHERE athlete_id = ?", A1)[0][0] for t in ("recording_athletes", "recording_physiological_samples", "recording_tracking_samples", "recording_periods"))
        check("no rows of the removed athlete remain", 0, left)
        dest.start()
        with httpx.Client(timeout=30) as c:
            d = dest.base_url
            kept = c.get(f"{d}/api/v1/recordings/{new_r1}/athletes/{A2}/recorded-samples.jsonl").text.splitlines() == [json.dumps(x, ensure_ascii=False) for x in rows[A2]]
            kept_batch = [strip(i["review"]) for i in c.get(f"{d}/api/v1/recordings/{new_r1}/athletes/{A2}/period-reviews").json()["items"]] == [strip(i["review"]) for i in dbatch[A2]["items"]]
            gone = c.get(f"{d}/api/v1/recordings/{new_r1}/athletes/{A1}/period-reviews").status_code
            check("the other athlete's samples and reviews are intact; the removed one is no longer a member", [True, True, 404], [kept, kept_batch, gone])
        dest.stop()

        # ---- recording removal on the stopped source ----
        rolling_before = {t: query(source.db_path, f"SELECT * FROM {t} ORDER BY 1, 2") for t in ("athletes", "physiological_samples", "tracking_samples")}
        code, _ = cli(local_data.main, "remove-recording", r1, "--database", source.db_path, "--execute", "--confirm", r1)
        check("recording removal executes", 0, code)
        check("removed recording leaves no rows", 0, sum(query(source.db_path, f"SELECT COUNT(*) FROM {t} WHERE recording_id = ?", r1)[0][0] for t in ("recording_athletes", "recording_physiological_samples", "recording_tracking_samples", "recording_periods", "recording_configurations")))
        check("rolling data and the registry unchanged", True, {t: query(source.db_path, f"SELECT * FROM {t} ORDER BY 1, 2") for t in rolling_before} == rolling_before)
        source.start()
        with httpx.Client(timeout=30) as c:
            check("the other recording is intact", r2_jsonl, c.get(f"{source.base_url}/api/v1/recordings/{r2}/athletes/{A3}/recorded-samples.jsonl").text)
        source.stop()

        # ---- a database with the tables as written before this batch ----
        older = Backend()
        try:
            con = sqlite3.connect(older.db_path)
            con.executescript(OLDER_SCHEMA)
            con.execute("INSERT INTO athletes VALUES (?, 'Fictional Older', 9, 'fictional', 'eval')", ("older-01",))
            con.execute("INSERT INTO recordings VALUES ('rec-older', 'older format', ?, ?)", (at(0).isoformat(timespec="microseconds"), at(30).isoformat(timespec="microseconds")))
            con.execute("INSERT INTO recording_athletes VALUES ('rec-older', 'older-01', 'Fictional Older', 9, 'fictional', 'eval')")
            for s in range(10):
                payload = json.dumps({"athlete_id": "older-01", "timestamp": z(at(s)), "source": "simulated", "device_id": "w", "heart_rate_bpm": None if s == 0 else 100.0 + s,
                                      "breathing_rate_bpm": None, "hrv_rmssd_ms": None, "hrv_sdnn_ms": None, "activity_level": None, "posture": None, "skin_temperature_c": None, "spo2_percent": None, "signal_quality": None})
                con.execute("INSERT INTO recording_physiological_samples VALUES ('rec-older', 'older-01', ?, ?)", (at(s).isoformat(timespec="microseconds"), payload))
            con.commit(); con.close()
            older.start()
            with httpx.Client(timeout=30) as c:
                od = c.get(f"{older.base_url}/api/v1/recordings/rec-older").json()
                check("older database: snapshot unavailable, receipts absent, samples readable", ["unavailable", 10, 0], [od["configuration_at_start"]["status"], od["recorded"][0]["physiological"]["sample_count"], od["recorded"][0]["physiological"]["receipt_count"]])
                opkg = c.get(f"{older.base_url}/api/v1/recordings/rec-older/package.zip").content
            with zipfile.ZipFile(io.BytesIO(opkg)) as zf:
                check("older database exports a package with no snapshot and no receipts", [None, b""], [json.loads(zf.read("recording.json"))["configuration_at_start"], zf.read("receipts.jsonl")])
            older.stop()
            code, _ = cli(local_data.main, "remove-recording", "rec-older", "--database", older.db_path, "--execute", "--confirm", "rec-older")
            check("older database: recording removal works", [0, 0], [code, query(older.db_path, "SELECT COUNT(*) FROM recording_physiological_samples")[0][0]])
        finally:
            older.cleanup()
    finally:
        source.cleanup()
        dest.cleanup()

    unknowns.append("Receipt bounds are checked against this script's clock on the same machine; with a separate sender or another computer, source-to-receipt differences mix delivery delay with clock offset.")
    return {"tool": "verify_evaluation_workflow", "generated_at": datetime.now(UTC).isoformat(), "fictional_data": True,
            "checks": check.items, "failures": [i["check"] for i in check.items if not i["passed"]], "unknowns": unknowns,
            "measured_on_this_machine": {"posts": len(posts), "server_timing_ms": {"median": server_ms[len(server_ms) // 2], "max": server_ms[-1]},
                                         "client_round_trip_ms": {"median": rtts[len(rtts) // 2], "max": rtts[-1]}},
            "limitations": ["Fictional data on one machine through localhost; not a device, network, display-timing or physiology check.",
                            "Removal is logical deletion in one database file; exported files, packages, backups and logs elsewhere are not reached."],
            "passed": all(i["passed"] for i in check.items)}


def main(argv: list[str] | None = None, *, out: Callable[[str], None] = print, backend_factory: Callable = Backend) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--report", type=Path, default=None, help="JSON report path (default reports/evaluation-workflow-<UTC time>.json, ignored by git)")
    args = p.parse_args(argv)

    def interrupt(*_):
        raise KeyboardInterrupt
    previous = {s: signal.signal(s, interrupt) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        with tempfile.TemporaryDirectory(prefix="athletrix-evaluation-") as tmp:
            report = run(out, Path(tmp), backend_factory)
    except KeyboardInterrupt:
        out("interrupted: the isolated backends and temporary files of this run were removed")
        return 130
    finally:
        for s, h in previous.items():
            signal.signal(s, h)
    path = args.report or (ROOT / "reports" / f"evaluation-workflow-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    out("EVALUATION WORKFLOW CHECK (fictional data, isolated engines)")
    for i in report["checks"]:
        out(f"  {'PASS' if i['passed'] else 'FAIL'}  {i['check']}" + ("" if i["passed"] else f"  (expected {i['expected']!r}, got {i['actual']!r})"))
    m = report["measured_on_this_machine"]
    out(f"  measured: {m['posts']} POSTs · Server-Timing median {m['server_timing_ms']['median']:.2f} ms (max {m['server_timing_ms']['max']:.2f}) · client round trip median {m['client_round_trip_ms']['median']:.2f} ms (max {m['client_round_trip_ms']['max']:.2f})")
    for u in report["unknowns"]:
        out("  unknown: " + u)
    out(f"JSON report: {path}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
