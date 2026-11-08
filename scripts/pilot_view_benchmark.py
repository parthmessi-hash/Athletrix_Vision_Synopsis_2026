"""LONG-HISTORY BENCHMARK of the fatigue pilot view (GET .../study/pilot): how its time grows late in a session.

USAGE
    .venv/bin/python scripts/pilot_view_benchmark.py --package EXPORT.zip --out DIR [--polls 7] [--label before]

THE FIXTURE: an exported recording package (for example the FICTIONAL 90-minute rehearsal's export) is imported into an
isolated temporary database and REOPENED as if live (its stop time is cleared in that copy only), so the live path,
including readiness, runs. The engine clock is frozen at three points of the session: early (10 min after the first
round start), middle (45 min) and late (15 s before the last wearer assignment ends, or the stop). This is a benchmark populated with historical data,
NOT another live run: nothing is paced, and the API is called in-process through the ASGI test client.

AT EACH POINT, for each trend setting (the dashboard's 300 s / 15 s, the rehearsal poller's 120 s / 15 s, and no trend):
  idle    N polls one second apart with no new data (time passes; freshness must still change)
  ingest  N polls one second apart, each after one second of new FICTIONAL heart rate and camera samples for the wearer
Reported per setting: response median and p95 (ms), score calculations per poll, and the time inside each stage
(inclusive, per poll): database reads (recording_store.range_with_receipts), evidence selection (fatigue.select),
input fingerprints (fatigue.digest), score calculations (fatigue.score, which contains selection and fingerprints),
readiness, plus the samples loaded per read. Output: DIR/benchmark.json and DIR/benchmark.md.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SPANS = {"dashboard": 300, "rehearsal": 120, "none": 0}
SETTINGS = [("dashboard 300 s / 15 s", 300, 15), ("rehearsal poller 120 s / 15 s", 120, 15), ("no trend", 0, 15)]


def pct(v, q):
    s = sorted(v)
    return s[min(len(s) - 1, int(q * len(s)))] if s else None


class Meter:
    def __init__(self):
        self.reset()

    def reset(self):
        self.t = {"db_read": 0.0, "select": 0.0, "digest": 0.0, "score": 0.0, "readiness": 0.0}
        self.n = {"db_read": 0, "select": 0, "digest": 0, "score": 0, "readiness": 0}
        self.rows = 0

    def wrap(self, name, fn, rows=False):
        def inner(*a, **k):
            t0 = time.perf_counter()
            try:
                r = fn(*a, **k)
                if rows:
                    self.rows += len(r)
                return r
            finally:
                self.t[name] += time.perf_counter() - t0
                self.n[name] += 1
        return inner


@contextmanager
def instrument(m: Meter):
    from app.api import study  # noqa: PLC0415
    from app.core import fatigue  # noqa: PLC0415
    saved = (study.recording_store.range_with_receipts, fatigue.select, fatigue.digest, fatigue.score, study.score, study.readiness)
    store = study.recording_store
    study.recording_store.range_with_receipts = m.wrap("db_read", saved[0], rows=True)
    fatigue.select = m.wrap("select", saved[1])
    fatigue.digest = m.wrap("digest", saved[2])
    fatigue.score = study.score = m.wrap("score", saved[3])
    study.readiness = m.wrap("readiness", saved[5])
    try:
        yield
    finally:
        store.range_with_receipts, fatigue.select, fatigue.digest, fatigue.score, study.score, study.readiness = saved
        # the instance attribute shadows the class method; remove it so the store is untouched
        store.__dict__.pop("range_with_receipts", None)


def main(argv=None, out=print) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--package", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--polls", type=int, default=7)
    ap.add_argument("--label", default="")
    ap.add_argument("--point", choices=["early", "middle", "late"], action="append", help="only these points (repeatable)")
    ap.add_argument("--setting", choices=["dashboard", "rehearsal", "none"], action="append", help="only these trend settings (repeatable)")
    args = ap.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("FATIGUE_PROFILE_PATH", str(ROOT / "scripts/examples/pilot/fictional_rehearsal_profile.json"))
    from fastapi.testclient import TestClient  # noqa: PLC0415

    from app.api import study as S  # noqa: PLC0415
    from app.core import clock  # noqa: PLC0415
    from app.core.config import get_settings  # noqa: PLC0415
    from app.core.recording_package import read_package  # noqa: PLC0415
    from app.db import Database, get_database, set_database  # noqa: PLC0415
    from app.main import app  # noqa: PLC0415
    from app.store.recording_import import import_package  # noqa: PLC0415
    get_settings.cache_clear()
    pkg = read_package(args.package.read_bytes())
    tmp = Path(tempfile.mkdtemp(prefix="athletrix-bench-"))
    db = Database(tmp / "bench.db")
    prev = None
    try:
        prev = get_database()
    except Exception:  # noqa: BLE001
        prev = None
    set_database(db)
    saved_clock = (clock.utc_now, clock.receipt_now)
    try:
        rid = import_package(db, pkg, clock.utc_now()).recording_id
        rec = S.require_recording(rid)
        stop = rec.stopped_at
        with db.transaction() as c:   # reopen THIS COPY as if live, so readiness and the live path run
            c.execute("UPDATE recordings SET stopped_at = NULL WHERE id = ?", (rid,))
        entries = S.study_store.list(rid)
        first_round = min(e.at for e in entries if e.kind == "round_start")
        ends = [w["end"] for w in S.wearer_intervals(S.current(entries)) if w["end"] is not None]
        last = min(stop, max(ends)) if ends and max(ends) > first_round + timedelta(minutes=60) else stop
        points = {"early (10 min)": first_round + timedelta(minutes=10), "middle (45 min)": first_round + timedelta(minutes=45),
                  "late (end)": last - timedelta(seconds=15)}
        st = {"now": None}
        clock.utc_now = lambda: st["now"]
        clock.receipt_now = lambda: st["now"]
        client = TestClient(app)
        m = Meter()
        results = []
        for pname, t0 in points.items():
            if args.point and pname.split()[0] not in args.point:
                continue
            live = [w for w in S.wearer_intervals(S.current(entries, t0)) if w["start"] <= t0 and (w["end"] is None or t0 < w["end"])]
            wearer = live[0]["entry"] if live else None
            for sname, span, step in SETTINGS:
                if args.setting and span not in [SPANS[x] for x in args.setting]:
                    continue
                for mode in ("idle", "ingest"):
                    lat, stages, counts, rows = [], [], [], []
                    st["now"] = t0
                    for k in range(args.polls):
                        st["now"] = t0 + timedelta(seconds=k + 1)
                        if mode == "ingest" and wearer is not None:
                            for j in range(10):
                                ts = (st["now"] - timedelta(seconds=1 - 0.1 * (j + 1))).isoformat().replace("+00:00", "Z")
                                if j == 9:
                                    client.post("/api/v1/samples/physiological", json={"athlete_id": wearer.athlete_id, "timestamp": ts, "source": "simulated",
                                                                                      "device_id": wearer.device_id, "heart_rate_bpm": 150.0})
                                client.post("/api/v1/samples/tracking", json={"athlete_id": wearer.athlete_id, "timestamp": ts, "x_position": 0.0, "y_position": 0.0,
                                                                             "speed_mps": 3.0, "acceleration_mps2": 0.0, "tracking_confidence": 0.9, "source": "camera",
                                                                             "segment_id": f"bench-{wearer.athlete_id}", "value_basis": "estimated"})
                        m.reset()
                        with instrument(m):
                            a = time.perf_counter()
                            r = client.get(f"/api/v1/recordings/{rid}/study/pilot", params={"trend_span_s": span, "trend_step_s": step})
                            lat.append(1000 * (time.perf_counter() - a))
                        assert r.status_code == 200, r.text
                        stages.append({k: 1000 * v for k, v in m.t.items()})
                        counts.append(dict(m.n))
                        rows.append(m.rows)
                    results.append({"point": pname, "at": t0.isoformat(), "setting": sname, "mode": mode, "polls": args.polls,
                                    "ms_median": round(statistics.median(lat), 1), "ms_p95": round(pct(lat, 0.95), 1),
                                    "score_calls_per_poll": statistics.median(c["score"] for c in counts),
                                    "rows_read_per_poll": statistics.median(rows),
                                    "stage_ms_median": {k: round(statistics.median(s[k] for s in stages), 1) for k in stages[0]},
                                    "wearer": wearer.athlete_id if wearer else None})
                    x = results[-1]
                    out(f"{pname:16} {sname:30} {mode:6} median {x['ms_median']:8.1f} ms  p95 {x['ms_p95']:8.1f} ms  scores {x['score_calls_per_poll']:4}  rows {x['rows_read_per_poll']:7}  "
                        + " ".join(f"{k} {v}" for k, v in x["stage_ms_median"].items()))
    finally:
        clock.utc_now, clock.receipt_now = saved_clock
        set_database(prev)
        db.close()
    doc = {"label": f"LONG-HISTORY BENCHMARK ({args.label}): the package's history imported into an isolated database and reopened as live in that copy; "
                    "in-process ASGI calls; not a live run", "package": args.package.name, "polls": args.polls, "results": results}
    (args.out / "benchmark.json").write_text(json.dumps(doc, indent=1, default=str))
    L = [f"# Pilot view benchmark ({args.label})", "", doc["label"], "", "| point | trend | mode | median ms | p95 ms | scores/poll | rows/poll | db read | select | fingerprints | score (incl.) | readiness |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for x in results:
        s = x["stage_ms_median"]
        L.append(f"| {x['point']} | {x['setting']} | {x['mode']} | {x['ms_median']} | {x['ms_p95']} | {x['score_calls_per_poll']} | {x['rows_read_per_poll']} | "
                 f"{s['db_read']} | {s['select']} | {s['digest']} | {s['score']} | {s['readiness']} |")
    (args.out / "benchmark.md").write_text("\n".join(L) + "\n")
    out(f"wrote {args.out / 'benchmark.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
