"""Guided foreground display check: engine receipt -> visible dashboard update, measured only while Chrome is drawing.

USAGE (one command; then one click in Chrome)
    .venv/bin/python scripts/display_check.py --out DIR [--duration-s 180] [--setting field-rehearsal|none]
                     [--min-visible 0.9] [--frontend "~/Athletrix Fixed Website"]
  1. SETUP (nothing is measured yet): an isolated engine with a FICTIONAL session, the timing proxy, and a local copy
     of the dashboard with a yellow banner. With --setting field-rehearsal (default) the GENERATED clip is processed
     once with YOLOX-Tiny 416, every other frame, as in scripts/video_pilot_check.py (needs models/yolox_tiny.onnx;
     without it the camera part is skipped and said so).
  2. It prints ONE address. Open it in Chrome, bring the window to the front (nothing covering it), and click START in
     the banner. START is enabled only while the page is drawing at least 30 frames per second.
  3. RECORDING (--duration-s, default 3 minutes): FICTIONAL heart rate at 1 Hz and the camera samples from the processed
     clip replayed at their own pace (looped) arrive for one wearer; the dashboard polls once a second as usual. The page
     records when each update became visible and a paint heartbeat (frames actually drawn). Keep the window in front.
  4. RESULT, saved even if visibility fails: DIR/trace.json (display log and heartbeat), DIR/pilot-package.zip (engine
     receipts), DIR/api_latency.json (API time, measured separately by a script), DIR/report.json and report.md.
     COMPLETE only when Chrome drew frames for at least --min-visible of the recording; otherwise INCOMPLETE, with the
     visible time and every sample that could not be observed counted, never dropped from the percentages.

A longer run: --duration-s 1800. Nothing changes power or display settings; if the display may sleep during a long
run, wrap the command yourself: caffeinate -d .venv/bin/python scripts/display_check.py ... (macOS; ends with it).
EXIT CODES: 0 COMPLETE · 1 INCOMPLETE (not enough visible time) · 2 not started or failed (files kept)
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

PY = sys.executable
VEST = "fictional-display-check-vest"

PAGE_JS = r"""
(function () {
  var P = window.__DC_PROXY, beats = [], frames = [], last = 0, phase = "waiting", startedAt = null;
  window.__beats = beats;
  (function f() { var n = Date.now(); frames.push(n); while (frames.length && n - frames[0] > 2000) frames.shift();
    if (n - last >= 250) { beats.push(n); last = n; } requestAnimationFrame(f); })();
  var bar = document.createElement("div");
  bar.style.cssText = "position:fixed;left:0;right:0;top:0;z-index:2147483647;background:#facc15;color:#111;font:600 15px system-ui;padding:10px 14px;display:flex;gap:12px;align-items:center;flex-wrap:wrap";
  var msg = document.createElement("span"), btn = document.createElement("button");
  btn.textContent = "START"; btn.style.cssText = "font:700 15px system-ui;padding:6px 18px"; btn.disabled = true; btn.id = "dcStart";
  bar.appendChild(btn); bar.appendChild(msg); document.body.appendChild(bar);
  function send(path, body) { return fetch(P + path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }).catch(function () {}); }
  function trace(final) { return send("/__display_log", { display: window.__athletrixPilotDisplay || [], painted: beats, final: !!final, sent_at: Date.now() }); }
  btn.onclick = function () {
    var fps = frames.length / 2;
    if (phase !== "waiting" || fps < 30 || document.visibilityState !== "visible") return;
    phase = "requested"; btn.disabled = true; startedAt = Date.now();
    send("/__start", { fps: fps, visibility: document.visibilityState, focus: document.hasFocus(), at: startedAt });
  };
  setInterval(function () {
    var fps = frames.length / 2;
    if (phase === "waiting") {
      var ready = fps >= 30 && document.visibilityState === "visible";
      btn.disabled = !ready;
      msg.textContent = ready ? "FICTIONAL display check: Chrome is drawing (" + Math.round(fps) + " frames/s). Click START, then leave this window in front and uncovered."
                              : "Bring this Chrome window to the front, uncovered (drawing " + Math.round(fps) + " frames/s; START needs 30).";
    }
  }, 300);
  setInterval(function () { if (phase === "recording") trace(false); }, 10000);
  setInterval(function () {
    fetch(P + "/__status", { cache: "no-store" }).then(function (r) { return r.json(); }).then(function (s) {
      if (s.phase === "recording" && phase !== "done") {
        phase = "recording";
        var left = Math.max(0, Math.round((s.ends_at_ms - Date.now()) / 1000));
        msg.textContent = "RECORDING (FICTIONAL): keep this window in front and uncovered for " + left + " s more. Frames drawn now: " + Math.round(frames.length / 2) + "/s.";
        bar.style.background = "#22c55e";
      } else if (s.phase === "done" && phase !== "done") {
        phase = "done"; trace(true); bar.style.background = "#93c5fd";
        msg.textContent = "DONE: the result is in the terminal. You can close this tab.";
      }
    }).catch(function () {});
  }, 1000);
})();
"""


def compute(display: list, receipts_ms: list[float], beats: list[float], start_ms: float, end_ms: float, tail_s: float = 5.0,
            min_visible: float = 0.9, max_gap_ms: float = 1000.0) -> dict:
    """Receipt -> first visible update covering it, only where the page drew frames throughout; everything else is
    counted. Visible share is measured over the recording interval itself, not over the span of received updates."""
    from pilot_display_delay import painted_spans  # noqa: PLC0415
    spans = painted_spans(beats, max_gap_ms)
    window_end = end_ms - 1000 * tail_s
    rec = sorted(r for r in (float(int(x)) for x in receipts_ms) if start_ms <= r <= window_end)
    disp = sorted(display)
    best, cov = [], -1e18
    for d in disp:
        cov = max(cov, d[1])
        best.append((d[0], cov))
    measured, unobserved, no_update, used = [], 0, 0, []
    j = 0
    for r in rec:
        while j < len(best) and best[j][1] < r:
            j += 1
        if j >= len(best):
            if any(a <= r <= b for a, b in spans):
                no_update += 1        # the page was drawing, yet no update showed it
            else:
                unobserved += 1       # received while the window was not drawn (updates stop when Chrome throttles it)
            continue
        shown = best[j][0]
        if any(a <= r and shown <= b for a, b in spans):
            measured.append((shown - r) / 1000)
            used.append(shown)
        else:
            unobserved += 1
    visible_ms = sum(max(0.0, min(b, end_ms) - max(a, start_ms)) for a, b in spans)
    total_ms = max(1.0, end_ms - start_ms)
    share = visible_ms / total_ms
    s = sorted(measured)
    within = sum(1 for x in s if x <= 2.0)
    complete = share >= min_visible and len(rec) > 0
    return {"verdict": "COMPLETE" if complete else "INCOMPLETE",
            "why": None if complete else (f"Chrome drew frames for {100 * share:.0f} % of the recording (needed {100 * min_visible:.0f} %)" if rec
                                          else "no heart-rate receipts in the recording interval"),
            "recording_s": round(total_ms / 1000, 1), "visible_s": round(visible_ms / 1000, 1), "visible_share": round(share, 3), "drawn_spans": len(spans),
            "samples": len(rec), "measured": len(measured), "unobserved_window_not_drawn": unobserved, "never_covered_by_an_update": no_update,
            "coalesced": len(used) - len(set(used)), "updates": len(disp),
            "delay_s": None if not s else {"median": round(statistics.median(s), 3), "p95": round(s[min(len(s) - 1, int(0.95 * len(s)))], 3), "max": round(s[-1], 3)},
            "within_2s_of_measured": None if not s else round(within / len(s), 4),
            "within_2s_of_all_samples": None if not rec else round(within / len(rec), 4),
            "rules": ["a sample is measured only if the page drew frames throughout, from its receipt to the visible update that covered it",
                      "unobserved and never-covered samples stay in 'within_2s_of_all_samples' as not shown within 2 s",
                      "several samples shown by one update are 'coalesced' (polling once a second)",
                      "API response time is in api_latency.json, never mixed into the delay"]}


def report_md(r: dict) -> str:
    d = r.get("delay_s") or {}
    L = ["# Foreground display check", "", f"**{r['verdict']}**" + (f": {r['why']}" if r.get("why") else ""), "",
         f"- Recording {r['recording_s']} s; Chrome drew frames for {r['visible_s']} s ({100 * r['visible_share']:.0f} %) in {r['drawn_spans']} span(s).",
         f"- Heart-rate samples {r['samples']}: measured {r['measured']}, unobserved (window not drawn) {r['unobserved_window_not_drawn']}, "
         f"never shown although the page was drawing {r['never_covered_by_an_update']}; coalesced {r['coalesced']}; updates {r['updates']}.",
         f"- Receipt -> visible update (measured samples only): median {d.get('median')} s, p95 {d.get('p95')} s, max {d.get('max')} s.",
         f"- Within 2 s: {r['within_2s_of_measured']} of measured samples; {r['within_2s_of_all_samples']} of ALL samples (unmeasured count as not within).",
         f"- API time (script, separate): {json.dumps(r.get('api_latency_ms'))}", "", "Inputs are FICTIONAL; camera samples come from a GENERATED clip."]
    return "\n".join(L) + "\n"


def main(argv=None, out=print) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--duration-s", type=float, default=180.0)
    ap.add_argument("--setting", choices=["field-rehearsal", "none"], default="field-rehearsal")
    ap.add_argument("--min-visible", type=float, default=0.9)
    ap.add_argument("--frontend", type=Path, default=Path.home() / "Athletrix Fixed Website")
    ap.add_argument("--wait-start-s", type=float, default=900.0, help="how long to wait for START before giving up")
    ap.add_argument("--dashboard-port", type=int, default=0); ap.add_argument("--proxy-port", type=int, default=0); ap.add_argument("--engine-port", type=int, default=0)
    args = ap.parse_args(argv)
    d = args.out
    if d.exists() and any(d.iterdir()):
        out(f"REFUSED: {d} is not empty"); return 2
    d.mkdir(parents=True, exist_ok=True)
    if not (args.frontend / "index.html").is_file():
        out(f"REFUSED: no dashboard at {args.frontend}/index.html (use --frontend)"); return 2
    import live_session  # noqa: PLC0415
    import video_pilot_check as vpc  # noqa: PLC0415
    from validate_team_scale import IsolatedBackend, free_port  # noqa: PLC0415
    eport, pport, dport = args.engine_port or free_port(), args.proxy_port or free_port(), args.dashboard_port or free_port()
    origin = f"http://127.0.0.1:{dport}"
    ctl = d / "control"
    ctl.mkdir()
    procs: list = []
    stop = threading.Event()
    status = lambda **kw: (ctl / "status.json").write_text(json.dumps(kw))  # noqa: E731
    status(phase="setup")
    result: dict = {"label": "FICTIONAL inputs; camera samples from a GENERATED clip", "setting": args.setting, "duration_s": args.duration_s}
    # camera part: process the generated clip once (field-rehearsal), before anything is measured
    cam = None
    if args.setting == "field-rehearsal":
        model = ROOT / "models" / "yolox_tiny.onnx"
        if not model.is_file():
            result["camera"] = "SKIPPED: models/yolox_tiny.onnx is not on this laptop (never downloaded); heart rate only"
        else:
            out("setup: processing the generated clip with field-rehearsal (YOLOX-Tiny 416, every other frame)...")
            from camera_adapter import synthetic_people as sp  # noqa: PLC0415
            from camera_adapter.evaluation import PEOPLE_CHECK_MARKS, PEOPLE_FIT_MARKS  # noqa: PLC0415
            from camera_adapter import observations as obsmod  # noqa: PLC0415
            camera = sp.Camera3()
            clip = d / "synthetic-people.avi"
            sp.render(clip, camera)
            mark = lambda x, y: {"label": f"mark ({x}, {y})", "image": [round(c) for c in camera.project(x, y)], "field": [x, y]}  # noqa: E731
            (d / "points.json").write_text(json.dumps({"format": "athletrix-calibration-points", "format_version": 1, "camera_id": "synthetic-people-camera",
                                                       "image": {"width": sp.WIDTH, "height": sp.HEIGHT}, "fit_points": [mark(*m) for m in PEOPLE_FIT_MARKS],
                                                       "check_points": [mark(*m) for m in PEOPLE_CHECK_MARKS]}))
            steps: list = []
            cfg = json.loads((ROOT / "scripts/examples/camera/adapter_config_example.json").read_text())
            cfg.update(device_id="synthetic-people-cam-1", source="simulated")
            (d / "adapter-config.json").write_text(json.dumps(cfg))
            cs = vpc.CAMERA_SETTINGS["field-rehearsal"]
            ok = not vpc.sh([PY, "scripts/camera_calibrate.py", "fit", str(d / "points.json"), "--out", str(d / "calibration.json")], steps) and \
                not vpc.sh([PY, "scripts/camera_track_video.py", str(clip), "--model", str(model), "--config", cs["config"], "--every-nth", str(cs["every_nth"]),
                            "--paced-replay", "--out", str(d / "obs.jsonl"), "--log", str(d / "track.json")], steps)
            if ok:
                obs = obsmod.load(d / "obs.jsonl")
                vpc.ATHLETES = {"red": "dx1"}
                vpc.write_assignments(d / "assign.json", vpc.operator(obs, sp, camera, reconfirm=True))
                cam = ["--calibration", str(d / "calibration.json"), "--config", str(d / "adapter-config.json"), "--assignments", str(d / "assign.json")]
                result["camera"] = {"setting": "field-rehearsal", "processing": json.loads((d / "track.json").read_text()).get("bounded", {}).get("frames")}
            else:
                result["camera"] = "SKIPPED: processing failed: " + "; ".join(steps[-1]["output_tail"])
    eng = IsolatedBackend(port=eport, extra_env={"FATIGUE_PROFILE_PATH": str(ROOT / "scripts/examples/pilot/fictional_rehearsal_profile.json")}, cors_origins=[origin])
    rest_id, bases = live_session.prepare_database(eng.db_path, datetime.now(UTC), players=[("dx1", "Fictional display-check participant", 91, False, None, [], True)])
    try:
        eng.start()
        c = httpx.Client(base_url=eng.base_url + "/api/v1", timeout=15.0)
        c.post("/athletes", json={"id": "dx1", "name": "Fictional display-check participant", "jersey_number": 91, "sport": "soccer", "team_id": "fictional-display"})
        rest = c.get(f"/recordings/{rest_id}").json()
        b = c.post("/athletes/dx1/baselines", json={"source_recording_id": rest_id, "ranges": [{"start": rest["started_at"], "end": rest["stopped_at"]}],
                                                     "condition": "seated_rest", "signals": ["heart_rate_bpm"], "min_samples": 60, "label": "FICTIONAL rest", "created_by": "display check"}).json()
        c.post(f"/athletes/dx1/baselines/{b['id']}/decision", json={"decision": "accepted", "decided_by": "display check", "note": "FICTIONAL"})
        rid = c.post("/recordings", json={"name": "FICTIONAL display check", "athlete_ids": ["dx1"]}).json()["id"]
        tok = iter(range(10000))
        entry = lambda **kw: c.post(f"/recordings/{rid}/study", json={"client_token": f"display-check-{next(tok):05d}", **kw})  # noqa: E731
        entry(kind="session", session_label="display-check", protocol="rehearsal", fictional=True,
              clock_references=[{"stream": "camera", "basis": "operator_estimate", "uncertainty_s": 0.04, "note": "FICTIONAL replay of a generated clip"}])
        entry(kind="wearer", athlete_id="dx1", participant_code="D1", device_id=VEST, baseline_id=b["id"], hr_max_bpm=200, hr_max_origin="FICTIONAL")
        procs.append(subprocess.Popen([PY, str(ROOT / "scripts" / "pilot_display_delay.py"), "proxy", "--engine", eng.base_url, "--port", str(pport),
                                       "--sink", str(d / "trace.json"), "--control", str(ctl)], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        web = d / "web"
        web.mkdir()
        html = (args.frontend / "index.html").read_text()
        html = html.replace("<head>", f'<head>\n<script>window.ATHLETRIX_CONFIG = {{ engineMode: "auto", apiBase: "http://127.0.0.1:{pport}" }}; '
                                      f'window.__DC_PROXY = "http://127.0.0.1:{pport}";</script>', 1)
        html = html.replace("</body>", '<script src="display-check.js"></script>\n</body>', 1)
        (web / "index.html").write_text(html)
        (web / "display-check.js").write_text(PAGE_JS)
        procs.append(subprocess.Popen([PY, "-m", "http.server", str(dport), "--bind", "127.0.0.1"], cwd=web, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        time.sleep(1.0)
        status(phase="waiting")
        url = f"{origin}/index.html#demo"
        out("")
        out(f"OPEN IN CHROME:  {url}")
        out("Bring that window to the front with nothing covering it, wait for the yellow banner to say 'Chrome is drawing',")
        out(f"then click START. The recording then runs {args.duration_s:g} s; keep the window in front until the banner turns blue.")
        (d / "url.txt").write_text(url + "\n")
        (d / "proxy.txt").write_text(f"http://127.0.0.1:{pport}\n")
        t_wait = time.monotonic()
        while not (ctl / "start.json").exists():
            if time.monotonic() - t_wait > args.wait_start_s:
                result.update(verdict="NOT STARTED", why=f"START was not clicked within {args.wait_start_s:g} s")
                (d / "report.json").write_text(json.dumps(result, indent=1)); out("NOT STARTED: nothing was measured"); return 2
            time.sleep(0.5)
        start_info = json.loads((ctl / "start.json").read_text())
        t_start = datetime.now(UTC)
        ends = t_start + timedelta(seconds=args.duration_s)
        status(phase="recording", ends_at_ms=ends.timestamp() * 1000)
        out(f"START received ({start_info.get('fps')} frames/s, {start_info.get('visibility')}); recording until {ends:%H:%M:%S} UTC")
        lat: list = []

        def feeder():
            k = 0
            while not stop.wait(1.0):
                t = datetime.now(UTC)
                c.post("/samples/physiological", json={"athlete_id": "dx1", "timestamp": t.isoformat().replace("+00:00", "Z"), "source": "simulated",
                                                       "device_id": VEST, "heart_rate_bpm": round(bases["dx1"]["hr"] + 55 + 10 * ((k // 20) % 2), 1)})
                k += 1

        def poller():
            with httpx.Client(timeout=15.0) as pc:
                while not stop.wait(1.0):
                    a = time.perf_counter()
                    r = pc.get(f"{eng.base_url}/api/v1/recordings/{rid}/study/pilot", params={"trend_span_s": 300, "trend_step_s": 15})
                    if r.status_code == 200:
                        lat.append(1000 * (time.perf_counter() - a))

        def camera_loop():
            import camera_to_samples  # noqa: PLC0415
            while not stop.is_set() and datetime.now(UTC) + timedelta(seconds=21) < ends:
                camera_to_samples.main(["send", str(d / "obs.jsonl"), *cam, "--anchor-utc", "now", "--url", eng.base_url, "--speed", "1"], out=lambda _: None)
        threads = [threading.Thread(target=feeder, daemon=True), threading.Thread(target=poller, daemon=True)]
        if cam:
            threads.append(threading.Thread(target=camera_loop, daemon=True))
        for th in threads:
            th.start()
        time.sleep(3.0)
        entry(kind="round_start", athlete_id="dx1", round=1, **({} if cam else {"partial_reason": "FICTIONAL display check without camera samples"}))
        while datetime.now(UTC) < ends:
            time.sleep(0.5)
        stop.set()
        t_end = datetime.now(UTC)
        status(phase="done")
        out("recording finished; waiting up to 90 s for the page's final trace...")
        t0 = time.monotonic()
        while time.monotonic() - t0 < 90:
            try:
                if json.loads((d / "trace.json").read_text()).get("final"):
                    break
            except (OSError, ValueError):
                pass
            time.sleep(1.0)
        c.post(f"/recordings/{rid}/stop")
        (d / "pilot-package.zip").write_bytes(c.get(f"/recordings/{rid}/package.zip").content)
        (d / "api_latency.json").write_text(json.dumps({"polls": len(lat), "ms": lat}))
    finally:
        stop.set()
        for pr in procs:
            pr.terminate()
        for pr in procs:
            try:
                pr.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pr.kill()
        eng.stop()
        eng.cleanup()
    try:
        tr = json.loads((d / "trace.json").read_text())
    except (OSError, ValueError):
        tr = {"display": [], "painted": [], "final": False}
    from app.core.recording_package import read_package  # noqa: PLC0415
    pkg = read_package((d / "pilot-package.zip").read_bytes())
    rec_ms = [datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp() * 1000 for (a, st, _), v in pkg.receipts.items() if st == "physiological"]
    r = compute(tr.get("display") or [], rec_ms, tr.get("painted") or [], t_start.timestamp() * 1000, t_end.timestamp() * 1000, min_visible=args.min_visible)
    s = sorted(lat)
    r["api_latency_ms"] = None if not s else {"polls": len(s), "p50": round(statistics.median(s)), "p95": round(s[int(0.95 * (len(s) - 1))]), "max": round(s[-1])}
    r["final_trace_received"] = bool(tr.get("final"))
    r.update({k: v for k, v in result.items() if k not in r})
    r["start"] = start_info
    (d / "report.json").write_text(json.dumps(r, indent=1, default=str))
    (d / "report.md").write_text(report_md(r))
    out(report_md(r))
    out(f"saved: {d}/report.md, trace.json, pilot-package.zip, api_latency.json")
    return 0 if r["verdict"] == "COMPLETE" else 1


if __name__ == "__main__":
    sys.exit(main())
