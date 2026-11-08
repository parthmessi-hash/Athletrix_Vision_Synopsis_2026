"""scripts/display_check.py: the guided foreground display check, tested with GENERATED traces and a scripted stand-in
for the browser (no real Chrome): visible share is measured over the recording, unobserved samples are counted and
kept in the all-samples percentage, and the run saves its trace, receipts and report even when visibility fails."""

import importlib.util
import json
import threading
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
import sys  # noqa: E402

sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("display_check", ROOT / "scripts" / "display_check.py")
dc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dc)


def trace(start, end, drawn, step=1000, lag=600):
    """Receipts every second; one update per second showing the newest receipt `lag` ms later; heartbeats only inside
    the drawn intervals."""
    rec = [start + k * step for k in range(int((end - start) / step))]
    disp = [[r + lag, r, 5] for r in rec]
    beats = [t for a, b in drawn for t in range(int(a), int(b), 250)]
    return disp, rec, beats


def test_complete_when_drawn_throughout():
    disp, rec, beats = trace(0, 120_000, [(0, 121_000)])
    r = dc.compute(disp, rec, beats, 0, 120_000)
    assert r["verdict"] == "COMPLETE" and r["visible_share"] >= 0.99 and r["measured"] == r["samples"] == 116   # receipts 0-115 s (the last 5 s are the tail)
    assert r["delay_s"]["median"] == 0.6 and r["within_2s_of_all_samples"] == 1.0 and r["unobserved_window_not_drawn"] == 0


def test_a_covered_window_makes_the_result_incomplete_and_stays_in_the_percentages():
    disp, rec, beats = trace(0, 120_000, [(0, 30_000), (90_000, 121_000)])   # covered for a minute in the middle
    r = dc.compute(disp, rec, beats, 0, 120_000)
    assert r["verdict"] == "INCOMPLETE" and "drew frames for 50 %" in r["why"]
    assert r["unobserved_window_not_drawn"] > 55 and r["measured"] + r["unobserved_window_not_drawn"] + r["never_covered_by_an_update"] == r["samples"]
    assert r["within_2s_of_measured"] == 1.0 and r["within_2s_of_all_samples"] < 0.55, "unmeasured samples are not silently dropped"


def test_updates_that_stop_while_hidden_do_not_shrink_the_window_and_coalescing_is_counted():
    rec = [k * 1000 for k in range(120)]
    disp = [[r + 600, r, 5] for r in rec if r < 60_000] + [[110_000, 109_000, 5]]   # hidden 60-110 s: one late update covers 50 samples
    beats = [t for t in range(0, 60_000, 250)] + [t for t in range(110_000, 121_000, 250)]
    r = dc.compute(disp, rec, beats, 0, 120_000)
    assert r["verdict"] == "INCOMPLETE" and 0.55 < r["visible_share"] < 0.65
    assert r["unobserved_window_not_drawn"] >= 49
    disp2 = [[r0 + 900, r0 + 500, 5] for r0 in range(0, 120_000, 2000)]   # one update every 2 s covers two receipts
    r2 = dc.compute(disp2, rec, [t for t in range(0, 121_000, 250)], 0, 120_000)
    assert r2["coalesced"] > 50 and r2["verdict"] == "COMPLETE"


def test_guided_run_with_a_scripted_browser_saves_everything(tmp_path):
    """End to end with --setting none: setup, wait for START, a short recording, final trace, report. The stand-in
    browser stops 'drawing' half way, so the result must be INCOMPLETE with its files saved."""
    out_dir = tmp_path / "dc"
    lines: list = []
    res: dict = {}
    th = threading.Thread(target=lambda: res.setdefault("code", dc.main(["--out", str(out_dir), "--duration-s", "12", "--setting", "none", "--wait-start-s", "60"],
                                                                          out=lines.append)))
    th.start()
    for _ in range(120):
        if (out_dir / "proxy.txt").exists():
            break
        time.sleep(0.25)
    proxy = (out_dir / "proxy.txt").read_text().strip()
    assert (out_dir / "url.txt").read_text().startswith("http://127.0.0.1:") and any(x.startswith("OPEN IN CHROME:") for x in lines)
    beats, disp = [], []
    with httpx.Client(timeout=10) as c:
        assert c.get(proxy + "/__status").json()["phase"] == "waiting"
        c.post(proxy + "/__start", json={"fps": 60, "visibility": "visible", "focus": True, "at": time.time() * 1000})
        rid = None
        t0 = time.time()
        while True:
            s = c.get(proxy + "/__status").json()
            if s.get("phase") == "done":
                break
            now = time.time() * 1000
            if time.time() - t0 < 6:           # drawn for the first ~6 s only
                beats.extend([now - 750, now - 500, now - 250, now])
            if rid is None:
                rid = (c.get(proxy + "/api/v1/recordings/active").json().get("recording") or {}).get("id")
            if rid:
                v = c.get(proxy + f"/api/v1/recordings/{rid}/study/pilot", params={"trend_span_s": 0}).json()
                if v.get("newest_receipt"):
                    from datetime import datetime
                    disp.append([time.time() * 1000, datetime.fromisoformat(v["newest_receipt"].replace("Z", "+00:00")).timestamp() * 1000, 5])
            time.sleep(1.0)
        c.post(proxy + "/__display_log", json={"display": disp, "painted": beats, "final": True})
    th.join(timeout=180)
    assert res.get("code") == 1, lines[-12:]
    rep = json.loads((out_dir / "report.json").read_text())
    assert rep["verdict"] == "INCOMPLETE" and rep["final_trace_received"] and rep["samples"] >= 5 and rep["measured"] >= 1
    assert rep["unobserved_window_not_drawn"] + rep["never_covered_by_an_update"] >= 1 and rep["api_latency_ms"]["polls"] >= 5
    for f in ("trace.json", "pilot-package.zip", "api_latency.json", "report.md"):
        assert (out_dir / f).exists(), f
