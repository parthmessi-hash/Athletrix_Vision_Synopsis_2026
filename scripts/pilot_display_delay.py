"""Arrival-to-display delay of the fatigue pilot view, and a loopback proxy to exercise slow and late responses.

WHAT IS MEASURED (and nothing more)
  t_receipt  the engine's receipt time of each heart-rate sample of the wearer (engine clock; receipt metadata).
  t_display  in the browser, the dashboard's pilot card records Date.now() in the first animation frame after it has
             put a response on the page, mapped onto the engine clock with an offset measured against GET /api/v1/clock
             (offset = engine time - midpoint of the probe; uncertainty = half the probe's round trip, stored per update).
             This is "DOM updated and a frame callback ran", not the moment light left the screen.
  A sample counts as displayed by the FIRST update whose response was calculated after the sample was received
  (newest receipt covered >= t_receipt); polling coalesces several samples into one update, and each of them gets that
  update's delay. Samples received while no update covered them (an outage, or the end of the window) are MISSING
  UPDATES. The scoring and polling path is the time between receipt and the response's calculation; the browser part is
  from the response to the frame. API response time alone is never used as display delay.

USAGE
    python scripts/pilot_display_delay.py proxy --engine URL --port 3919 [--slow-every 10 --slow-s 2.5] [--sink display.json]
    python scripts/pilot_display_delay.py analyze --display LOG.json --database ENGINE.db [--tail-s 5]
LOG.json is the browser's window.__athletrixPilotDisplay ([display ms on engine clock, newest receipt ms, uncertainty ms]).
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx


def proxy(args, out) -> int:
    count = {"n": 0}
    lock = threading.Lock()
    client = httpx.Client(timeout=30.0)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _sink(self, body: bytes | None):
            if self.command == "POST" and body is not None:
                Path(args.sink).write_bytes(body)
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.end_headers()

        def _go(self):
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0)) if self.command in ("POST", "PUT") else None
            if args.sink and self.path == "/__display_log":
                return self._sink(body)
            hdrs = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "content-length", "connection")}
            r = client.request(self.command, args.engine.rstrip("/") + self.path, headers=hdrs, content=body)
            if args.path_contains in self.path and self.command == "GET":
                with lock:
                    count["n"] += 1
                    slow = args.slow_every and count["n"] % args.slow_every == 0
                if slow:
                    time.sleep(args.slow_s)
            self.send_response(r.status_code)
            for k, v in r.headers.items():
                if k.lower() not in ("content-length", "transfer-encoding", "connection", "content-encoding"):
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(r.content)))
            self.end_headers()
            self.wfile.write(r.content)
        do_GET = do_POST = do_OPTIONS = _go
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), H)
    out(f"proxy http://127.0.0.1:{args.port} -> {args.engine}; every {args.slow_every or 'no'}th GET containing {args.path_contains!r} delayed {args.slow_s} s")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def analyze(display: list, receipts: list[float], tail_s: float = 5.0) -> dict:
    """display: [[t_display_ms, newest_receipt_ms, unc_ms], ...]; receipts: receipt times in ms (engine clock)."""
    if not display:
        return {"updates": 0, "note": "no display updates were recorded"}
    display = sorted(display)
    # the browser's Date.parse keeps whole milliseconds; compare receipts at the same resolution (found in the
    # 2026-11-08 run: a sample received at x.686198 s looked uncovered by an update covering x.686 s)
    receipts = [math.floor(r) for r in receipts]
    first, last = display[0][0], display[-1][0]
    window = [r for r in sorted(receipts) if first <= r <= last - 1000 * tail_s]
    delays, missing = [], 0
    j = 0
    best = []   # running maximum of covered receipt by display time
    cov = -1e18
    for d in display:
        cov = max(cov, d[1])
        best.append((d[0], cov))
    for r in window:
        while j < len(best) and best[j][1] < r:
            j += 1
        if j >= len(best):
            missing += 1
            continue
        delays.append((best[j][0] - r) / 1000)
    unc = max(d[2] for d in display) / 1000
    gaps = [(b[0] - a[0]) / 1000 for a, b in zip(display, display[1:])]
    s = sorted(delays)
    return {"samples": len(window), "displayed": len(delays), "missing_updates": missing, "updates": len(display),
            "delay_s": None if not s else {"median": round(statistics.median(s), 3), "p95": round(s[min(len(s) - 1, int(0.95 * len(s)))], 3), "max": round(s[-1], 3),
                                            "share_within_2s": round(sum(1 for x in s if x <= 2.0) / len(s), 4)},
            "clock_mapping_uncertainty_s": round(unc, 4), "longest_gap_between_updates_s": round(max(gaps), 3) if gaps else None,
            "definition": "engine receipt -> first animation frame after the dashboard showed a response calculated after that receipt (browser time mapped to the engine clock)"}


def main(argv=None, out=print) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("proxy"); p.add_argument("--engine", required=True); p.add_argument("--port", type=int, required=True)
    p.add_argument("--slow-every", type=int, default=0); p.add_argument("--slow-s", type=float, default=2.5); p.add_argument("--path-contains", default="/study/pilot")
    p.add_argument("--sink", help="write a POST to /__display_log (the browser's display log) to this local file")
    a = sub.add_parser("analyze"); a.add_argument("--display", type=Path, required=True); a.add_argument("--database", type=Path, required=True); a.add_argument("--tail-s", type=float, default=5.0)
    args = ap.parse_args(argv)
    if args.cmd == "proxy":
        return proxy(args, out)
    display = json.loads(args.display.read_text())
    con = sqlite3.connect(f"file:{args.database}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT json_extract(receipt, '$.received_at') FROM recording_physiological_samples WHERE receipt IS NOT NULL").fetchall()
    finally:
        con.close()
    rec = [datetime.fromisoformat(r[0].replace("Z", "+00:00")).timestamp() * 1000 for r in rows if r[0]]
    res = analyze(display, rec, args.tail_s)
    out(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
