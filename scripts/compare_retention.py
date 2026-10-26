"""Compare retention and effort review under two retention-cap profiles, on isolated backends.

WHAT IT DOES
  Runs the same fictional team-scale scenario twice (scripts/validate_team_scale.py machinery: same athletes, same
  deterministic sample values, same relative schedule, concurrent overview reads), once against an isolated backend
  started with profile A (the defaults: 300 physiological / 300 tracking samples per athlete) and once against another
  isolated backend started with profile B (default 300 / 3000, set with --physiological-cap / --tracking-cap). Each run
  keeps its own genuine timestamps; nothing is rebased. The normal development backend and database are never touched.

  After each run it reads the backend back and checks, per athlete:
    - retained counts and timestamp spans under that profile's caps, latest readings, athlete and device separation,
      and the recorded window and summary (the existing verification);
    - an early selected effort (default 5 s to 15 s after the run start) whose tracking samples have fallen outside the
      default tracking cap by the end of the run but remain under the larger cap: the review's movement figures
      (sample count, distance, included duration, high-speed time and distance) against expectations calculated
      independently here from the samples the backend accepted, under the newest-N rule of that profile; the
      Before / During / After heart-rate sample selection against the same samples; the review's availability
      metadata (cap, retained count and bounds, at_cap, selected count and bounds) against those facts; and that a
      missing distance stays null rather than becoming zero;
    - the recorded window over the effort, with its own availability metadata;
    - that the retained-samples export of one athlete holds every retained row, and that replaying it into a third
      isolated backend started with the same caps reproduces the same retained rows.
  The two runs are then compared: heart-rate periods must agree (same deterministic values), and the movement review
  must differ only in what the caps explain.

WHAT IT IS NOT
  Not a hardware test, not a capacity claim, not a match-length soak: 60 s on one machine. Latency and lateness are
  observations with no threshold. A timed-out request leaves acceptance unknown and is reported as unknown.

USAGE
    python scripts/compare_retention.py                                              # 20 athletes, 1 Hz / 10 Hz, 60 s, B = 300 / 3000
    python scripts/compare_retention.py --athletes 20 --duration 60 --tracking-cap 3000 --effort-start 5 --effort-end 15
    python scripts/compare_retention.py --report reports/my-comparison.json          # reports/ is ignored by git
"""

import argparse
import json
import math
import signal
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
for p in (str(ROOT), str(SCRIPTS)):
    if p not in sys.path:
        sys.path.insert(0, p)

import replay_samples  # noqa: E402
from validate_team_scale import (  # noqa: E402
    DEFAULT_RETENTION_CAP, STREAMS, IsolatedBackend, RunConfig, athlete_id, build_report, expected_retained, machine_context,
    print_summary, register_team, run_load, scenario_plan, verify, verify_overview,
)

DEFAULT_ATHLETES = 20
DEFAULT_DURATION_S = 60.0
DEFAULT_PHYSIOLOGICAL_RATE_HZ = 1.0
DEFAULT_TRACKING_RATE_HZ = 10.0
DEFAULT_EFFORT = (5, 15)            # seconds after the run start
DEFAULT_LARGER_TRACKING_CAP = 3000
# the backend's review rules, repeated here only to compute independent expectations; the backend's response echoes its own
BEFORE_S, AFTER_S = 10.0, 30.0
MAX_GAP_S, MIN_CONFIDENCE, HIGH_SPEED_MPS = 3.0, 0.5, 5.0
TOL = 1e-6


@dataclass(frozen=True)
class Effort:
    start_s: int
    end_s: int

    @property
    def duration_s(self) -> int:
        return self.end_s - self.start_s


def parse_ts(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def effort_outside_default_cap(cfg: RunConfig, effort: Effort, default_tracking_cap: int = DEFAULT_RETENTION_CAP) -> bool:
    """Whether every tracking sample of the effort is older than the newest `default_tracking_cap` samples at the end of the run."""
    total = max(1, int(round(cfg.duration_s * cfg.rate_for("tracking"))))
    oldest_retained_s = max(0, total - default_tracking_cap) / cfg.rate_for("tracking")
    return effort.end_s < oldest_retained_s


# ---- independent expectations -----------------------------------------------------------------------------------------

def samples_in(records_sorted: list, lo: datetime, hi: datetime, lo_inclusive: bool = True, hi_inclusive: bool = True) -> list:
    out = []
    for r in records_sorted:
        ts = parse_ts(r.payload["timestamp"])
        after_lo = ts >= lo if lo_inclusive else ts > lo
        before_hi = ts <= hi if hi_inclusive else ts < hi
        if after_lo and before_hi:
            out.append(r)
    return out


def independent_movement(tracking: list, *, threshold_mps: float = HIGH_SPEED_MPS, max_gap_s: float = MAX_GAP_S, min_confidence: float = MIN_CONFIDENCE) -> dict:
    """Distance and high-speed figures from tracking payloads sorted by timestamp, written here without the backend's code.

    Consecutive samples form an interval when 0 < elapsed <= max_gap_s and both confidences are at least min_confidence.
    Speed changes linearly inside an interval: distance is the trapezoid; the high-speed part is the sub-interval at or
    above the threshold, with the crossing interpolated. No eligible interval: distance and high-speed figures are None.
    """
    eligible = []
    excluded = 0
    for a, b in zip(tracking, tracking[1:]):
        dt = (parse_ts(b.payload["timestamp"]) - parse_ts(a.payload["timestamp"])).total_seconds()
        ca, cb = a.payload.get("tracking_confidence"), b.payload.get("tracking_confidence")
        if 0 < dt <= max_gap_s and ca is not None and cb is not None and ca >= min_confidence and cb >= min_confidence:
            eligible.append((a.payload["speed_mps"], b.payload["speed_mps"], dt))
        else:
            excluded += 1
    if not eligible:
        return {"sample_count": len(tracking), "estimated_distance_m": None, "included_duration_seconds": 0.0, "excluded_interval_count": excluded,
                "high_speed_duration_seconds": None, "high_speed_distance_m": None}
    distance = included = hs_t = hs_d = 0.0
    for v0, v1, dt in eligible:
        distance += (v0 + v1) / 2 * dt
        included += dt
        above0, above1 = v0 >= threshold_mps, v1 >= threshold_mps
        if above0 and above1:
            hs_t += dt; hs_d += (v0 + v1) / 2 * dt
        elif above0 != above1:
            t_c = dt * (threshold_mps - v0) / (v1 - v0)       # where the linear speed crosses the threshold
            if above1:
                hs_t += dt - t_c; hs_d += (threshold_mps + v1) / 2 * (dt - t_c)
            else:
                hs_t += t_c; hs_d += (v0 + threshold_mps) / 2 * t_c
    return {"sample_count": len(tracking), "estimated_distance_m": distance, "included_duration_seconds": included, "excluded_interval_count": excluded,
            "high_speed_duration_seconds": min(hs_t, included), "high_speed_distance_m": min(hs_d, distance)}


def expected_periods(phys_retained: list, effort_start: datetime, effort_end: datetime, captured_at: datetime) -> dict:
    """Before [start-10, start), during [start, end], after (end, min(end+30, captured_at)] over the retained physiological samples."""
    bounds = {
        "before": (effort_start - timedelta(seconds=BEFORE_S), effort_start, True, False),
        "during": (effort_start, effort_end, True, True),
        "after": (effort_end, min(effort_end + timedelta(seconds=AFTER_S), captured_at), False, True),
    }
    out = {}
    for name, (lo, hi, li, hi_inc) in bounds.items():
        chosen = samples_in(phys_retained, lo, hi, li, hi_inc)
        rates = [r.payload["heart_rate_bpm"] for r in chosen if r.payload.get("heart_rate_bpm") is not None]
        out[name] = {"sample_count": len(chosen), "first": chosen[0].payload["timestamp"] if chosen else None, "last": chosen[-1].payload["timestamp"] if chosen else None,
                     "mean": sum(rates) / len(rates) if rates else None}
    return out


def close(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= TOL * max(1.0, abs(a), abs(b))


def same_instant(a: str | None, b: str | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return parse_ts(a) == parse_ts(b)


def settle(ok: bool, uncertain: bool):
    """A mismatch that could be explained by a timed-out request is unknown (None), never a pass."""
    return None if (not ok and uncertain) else ok


# ---- checks against one backend -------------------------------------------------------------------------------------------

def check_effort(client, base_url: str, records: list, cfg: RunConfig, start_utc: datetime, effort: Effort, caps: dict[str, int]) -> dict:
    """Effort review, its availability metadata and the recorded window over the effort, per athlete, against independent expectations."""
    effort_start, effort_end = start_utc + timedelta(seconds=effort.start_s), start_utc + timedelta(seconds=effort.end_s)
    per_athlete = []
    overall = {"checked": 0, "passed": 0, "failed": 0, "unknown": 0}
    for i in range(cfg.athletes):
        a = athlete_id(i)
        timed_out = {s: any(r.athlete == a and r.stream == s and r.outcome == "timeout" for r in records) for s in STREAMS}
        checks: dict = {}
        figures: dict = {}
        try:
            body = client.get(f"{base_url}/api/v1/athletes/{a}/effort-review", params={"effort_start": effort_start.isoformat(), "effort_end": effort_end.isoformat()}).json()
            retained = {s: expected_retained(records, a, s, caps[s]) for s in STREAMS}
            tracking_in = samples_in(retained["tracking"], effort_start, effort_end)
            exp_move = independent_movement(tracking_in)
            m, av = body["movement"], body.get("availability") or {}
            u = timed_out["tracking"]
            checks["movement_sample_count"] = settle(m["sample_count"] == exp_move["sample_count"], u)
            checks["movement_distance"] = settle(close(m["estimated_distance_m"], exp_move["estimated_distance_m"]), u)
            checks["movement_included_duration"] = settle(close(m["included_duration_seconds"], exp_move["included_duration_seconds"]), u)
            checks["movement_high_speed_duration"] = settle(close(m["high_speed_duration_seconds"], exp_move["high_speed_duration_seconds"]), u)
            checks["movement_high_speed_distance"] = settle(close(m["high_speed_distance_m"], exp_move["high_speed_distance_m"]), u)
            checks["missing_distance_stays_null"] = (m["estimated_distance_m"] is None) if exp_move["sample_count"] < 2 else True
            mav = av.get("movement") or {}
            checks["movement_availability"] = settle(
                mav.get("max_samples") == caps["tracking"] and mav.get("retained_count") == len(retained["tracking"]) and mav.get("at_cap") == (len(retained["tracking"]) >= caps["tracking"])
                and mav.get("selected_count") == m["sample_count"] and same_instant(mav.get("retained_earliest"), retained["tracking"][0].payload["timestamp"] if retained["tracking"] else None)
                and same_instant(mav.get("retained_latest"), retained["tracking"][-1].payload["timestamp"] if retained["tracking"] else None)
                and same_instant(mav.get("selected_first"), tracking_in[0].payload["timestamp"] if tracking_in else None), u)
            captured_at = parse_ts(body["captured_at"])
            exp_hr = expected_periods(retained["physiological"], effort_start, effort_end, captured_at)
            up = timed_out["physiological"]
            hr = body["heart_rate"]
            for name in ("before", "during", "after"):
                p, e = hr[name], exp_hr[name]
                checks[f"heart_rate_{name}_selection"] = settle(p["sample_count"] == e["sample_count"] and same_instant(p["first_sample_at"], e["first"]) and same_instant(p["last_sample_at"], e["last"]) and close(p["heart_rate_mean_bpm"], e["mean"]), up)
                pav = (av.get("heart_rate") or {}).get(name) or {}
                checks[f"heart_rate_{name}_availability"] = settle(
                    pav.get("max_samples") == caps["physiological"] and pav.get("retained_count") == len(retained["physiological"]) and pav.get("selected_count") == p["sample_count"]
                    and pav.get("at_cap") == (len(retained["physiological"]) >= caps["physiological"]) and same_instant(pav.get("selected_last"), e["last"]), up)
            win = client.get(f"{base_url}/api/v1/athletes/{a}/window", params={"seconds": effort.duration_s, "end": effort_end.isoformat()}).json()
            wav = (win.get("availability") or {}).get("tracking") or {}
            checks["window_tracking_selection"] = settle(len(win["tracking_samples"]) == len(tracking_in) and wav.get("selected_count") == len(tracking_in) and wav.get("retained_count") == len(retained["tracking"]), u)
            figures = {"movement": {k: m.get(k) for k in ("sample_count", "estimated_distance_m", "included_duration_seconds", "high_speed_duration_seconds", "high_speed_distance_m")},
                       "expected_movement": exp_move,
                       "movement_availability": {k: mav.get(k) for k in ("max_samples", "retained_count", "retained_earliest", "at_cap", "selected_count")},
                       "heart_rate": {n: {"sample_count": hr[n]["sample_count"], "mean": hr[n]["heart_rate_mean_bpm"]} for n in ("before", "during", "after")},
                       "availability_explains_absence": (m["sample_count"] == 0 and mav.get("selected_count") == 0 and mav.get("retained_count", 0) > 0 and parse_ts(mav["retained_earliest"]) > effort_end) if m["sample_count"] == 0 and mav.get("retained_earliest") else None}
        except Exception as exc:  # noqa: BLE001
            checks["reads"] = False
            figures = {"error": f"{type(exc).__name__}: {exc}"}
        status = "failed" if any(v is False for v in checks.values()) else ("unknown" if any(v is None for v in checks.values()) else "passed")
        overall["checked"] += 1; overall[status] += 1
        per_athlete.append({"athlete_id": a, "status": status, "checks": checks, **figures})
    return {"effort": {"start_s": effort.start_s, "end_s": effort.end_s, "effort_start": effort_start.isoformat(), "effort_end": effort_end.isoformat()}, "overall": overall, "per_athlete": per_athlete}


def check_export_and_replay(client, base_url: str, athlete: str, records: list, caps: dict[str, int], replay_backend_factory: Callable, out: Callable[[str], None],
                            replay: Callable = None, client_factory: Callable = None) -> dict:
    """Export one athlete's retained samples, check every retained row is there, replay the file into a fresh isolated backend with the same caps and compare."""
    result: dict = {"athlete_id": athlete}
    exp = {s: expected_retained(records, athlete, s, caps[s]) for s in STREAMS}
    uncertain = any(r.athlete == athlete and r.outcome == "timeout" for r in records)
    r = client.get(f"{base_url}/api/v1/athletes/{athlete}/retained-samples.jsonl")
    if r.status_code != 200:
        return {**result, "checks": {"export_http": False}, "detail": f"HTTP {r.status_code}"}
    lines = r.text.splitlines()
    counts = {s: sum(1 for l in lines if json.loads(l)["type"] == s) for s in STREAMS}
    result["exported"] = counts
    result["headers"] = {k: r.headers.get(k) for k in ("X-Athletrix-Physiological-Count", "X-Athletrix-Tracking-Count", "X-Athletrix-Physiological-Cap", "X-Athletrix-Tracking-Cap")}
    checks = {"export_holds_every_retained_row": settle(all(counts[s] == len(exp[s]) for s in STREAMS), uncertain),
              "export_headers_name_the_caps": r.headers.get("X-Athletrix-Physiological-Cap") == str(caps["physiological"]) and r.headers.get("X-Athletrix-Tracking-Cap") == str(caps["tracking"])}
    with tempfile.TemporaryDirectory(prefix="athletrix-compare-") as tmp:
        path = Path(tmp) / "export.jsonl"
        path.write_text(r.text, encoding="utf-8")
        backend = replay_backend_factory(caps)
        try:
            backend.start()
            out(f"  replaying {len(lines)} exported records for {athlete} into a third isolated backend on port {backend.port} (same caps)…")
            code = (replay or replay_samples.main)([str(path), "--url", backend.base_url, "--speed", "1000"], out=lambda s: None)
            checks["replay_exit_code_zero"] = code == 0
            with (client_factory or (lambda: httpx.Client(timeout=10.0)))() as c:
                again = c.get(f"{backend.base_url}/api/v1/athletes/{athlete}/retained-samples.jsonl")
                rd = c.get(f"{backend.base_url}/api/v1/athletes/{athlete}/retained-data").json()
            checks["replayed_export_identical"] = again.status_code == 200 and again.text.splitlines() == lines
            checks["replayed_counts_match"] = all(rd[s]["sample_count"] == counts[s] for s in STREAMS)
            result["replayed"] = {s: rd[s]["sample_count"] for s in STREAMS}
        finally:
            backend.cleanup()
    result["checks"] = checks
    return result


# ---- one profile ---------------------------------------------------------------------------------------------------------------

def run_profile(name: str, caps: dict[str, int], cfg: RunConfig, effort: Effort, out: Callable[[str], None], backend_factory: Callable = IsolatedBackend) -> dict:
    cfg = RunConfig(**{**cfg.__dict__, "physiological_cap": caps["physiological"], "tracking_cap": caps["tracking"]})
    plan = scenario_plan(cfg.scenario, cfg.athletes, cfg)
    backend = backend_factory(retention_caps=caps)
    try:
        out(f"[{name}] starting an isolated backend on port {backend.port} with caps physiological {caps['physiological']} / tracking {caps['tracking']} (database {backend.db_path})…")
        backend.start()
        info = {"base_url": backend.base_url, "pid": backend.proc.pid if backend.proc else None, "db_path": str(backend.db_path), "command": backend.cmd, "retention_caps": caps,
                "retention_cap": caps["physiological"] if caps["physiological"] == caps["tracking"] else None}
        setup = register_team(backend.base_url, cfg, out)
        out(f"[{name}] sending {cfg.expected_samples} fictional samples for {cfg.athletes} athletes (physiological {cfg.rate_for('physiological'):g} Hz, tracking {cfg.rate_for('tracking'):g} Hz, overview read every {cfg.read_interval_s:g} s)…")
        res = run_load(backend.base_url, cfg, plan=plan)
        out(f"[{name}] reading back and verifying…")
        with httpx.Client(timeout=cfg.timeout_s) as client:
            verification = verify(client, backend.base_url, res.records, cfg, res.start_utc)
            overview = verify_overview(client, backend.base_url, res.records, res.statuses, cfg, plan)
            effort_check = check_effort(client, backend.base_url, res.records, cfg, res.start_utc, effort, caps)
            export_check = check_export_and_replay(client, backend.base_url, athlete_id(0), res.records, caps, lambda c: backend_factory(retention_caps=c), out)
        report = build_report(cfg, info, res.records, res.actual_s, res.start_utc, verification, reads=res.reads, statuses=res.statuses, setup=setup, overview_verification=overview, plan=plan)
        return {"name": name, "caps": caps, "report": report, "effort_check": effort_check, "export_check": export_check}
    finally:
        backend.cleanup()


# ---- comparison -------------------------------------------------------------------------------------------------------------------

def compare_profiles(a: dict, b: dict) -> dict:
    """Per athlete: what the review shows under each profile and whether the difference is the one the caps explain."""
    rows = []
    agree_hr = True
    for pa, pb in zip(a["effort_check"]["per_athlete"], b["effort_check"]["per_athlete"]):
        ma, mb = pa.get("movement") or {}, pb.get("movement") or {}
        ha, hb = pa.get("heart_rate") or {}, pb.get("heart_rate") or {}
        hr_same = all(ha.get(n, {}).get("sample_count") == hb.get(n, {}).get("sample_count") and close(ha.get(n, {}).get("mean"), hb.get(n, {}).get("mean")) for n in ("before", "during", "after"))
        agree_hr = agree_hr and hr_same
        rows.append({"athlete_id": pa["athlete_id"],
                     "A": {"status": pa["status"], "tracking_samples": ma.get("sample_count"), "distance_m": ma.get("estimated_distance_m"), "high_speed_s": ma.get("high_speed_duration_seconds"), "availability": pa.get("movement_availability")},
                     "B": {"status": pb["status"], "tracking_samples": mb.get("sample_count"), "distance_m": mb.get("estimated_distance_m"), "high_speed_s": mb.get("high_speed_duration_seconds"), "availability": pb.get("movement_availability")},
                     "heart_rate_periods_agree": hr_same,
                     "difference_explained_by_caps": (ma.get("sample_count") == 0 and ma.get("estimated_distance_m") is None and (pa.get("availability_explains_absence") is True) and (mb.get("sample_count") or 0) > 1 and mb.get("estimated_distance_m") is not None)})
    return {"per_athlete": rows, "heart_rate_periods_agree_for_all": agree_hr,
            "athletes_with_null_distance_under_A_and_a_value_under_B": sum(1 for r in rows if r["difference_explained_by_caps"]),
            "note": "Under A the effort's tracking samples are older than the newest 300 rows, so the review has no samples and a null (not zero) distance, and its availability metadata says the stream is at its cap with retained history beginning after the effort; under B the same effort is reviewed from retained rows. Heart-rate periods are expected to agree because both runs send the same deterministic values on the same relative schedule."}


def profile_summary(p: dict) -> dict:
    r = p["report"]
    return {"caps": p["caps"], "requests": r["requests"], "latency_s": r["latency_s"], "scheduling_lateness_s": r["scheduling_lateness_s"], "read_latency_s": r["read_latency_s"],
            "retained_spans": {s: {"rows_min": min(v["rows"]) if v["rows"] else None, "rows_max": max(v["rows"]) if v["rows"] else None, "span_s_min": min(x for x in v["span_s"] if x is not None) if any(x is not None for x in v["span_s"]) else None, "span_s_max": max(x for x in v["span_s"] if x is not None) if any(x is not None for x in v["span_s"]) else None} for s, v in r["retained_spans"].items()},
            "verification": r["verification"]["overall"], "overview_checks": r["overview_verification"].get("checks", {}), "reads": {k: r["reads"].get(k) for k in ("expected", "ok", "failed", "unsent", "checks_failed")},
            "effort_overall": p["effort_check"]["overall"], "export_checks": p["export_check"].get("checks", {}), "exported": p["export_check"].get("exported"), "replayed": p["export_check"].get("replayed")}


def build_comparison_report(cfg: RunConfig, effort: Effort, a: dict, b: dict) -> dict:
    return {
        "tool": "compare_retention", "generated_at": datetime.now(UTC).isoformat(), "fictional_data": True,
        "config": {"athletes": cfg.athletes, "requested_duration_s": cfg.duration_s, "physiological_rate_hz": cfg.rate_for("physiological"), "tracking_rate_hz": cfg.rate_for("tracking"), "read_interval_s": cfg.read_interval_s,
                   "effort": {"start_s": effort.start_s, "end_s": effort.end_s, "outside_default_tracking_cap_at_end_of_run": effort_outside_default_cap(cfg, effort)}},
        "machine": machine_context(),
        "summary": {"A": profile_summary(a), "B": profile_summary(b)},
        "comparison": compare_profiles(a, b),
        "profiles": {"A": a, "B": b},
        "limitations": [
            "Two short runs on one machine; observations only, no latency threshold, no capacity or match-length claim.",
            "The independent movement expectation re-implements the documented trapezoid and threshold-crossing rules from the accepted samples; it is a cross-check of the review, not a ground truth about any athlete.",
            "A timed-out request leaves acceptance unknown; the affected athlete's checks are reported as unknown, never as passed.",
            "All athletes, devices and values are fictional; the 10 Hz tracking stream is synthetic, not a camera integration.",
        ],
    }


def f_ms(x) -> str:
    return "n/a" if x is None else f"{x * 1000:.1f} ms"


def print_comparison(report: dict, out: Callable[[str], None] = print) -> None:
    c, s, cmp = report["config"], report["summary"], report["comparison"]
    out("RETENTION COMPARISON (fictional athletes, two isolated backends, same deterministic values and relative schedule)")
    out(f"Config: {c['athletes']} athletes · physiological {c['physiological_rate_hz']:g} Hz · tracking {c['tracking_rate_hz']:g} Hz · {c['requested_duration_s']:g} s · overview read every {c['read_interval_s']:g} s · effort {c['effort']['start_s']}-{c['effort']['end_s']} s after start (outside the default tracking cap at the end of the run: {c['effort']['outside_default_tracking_cap_at_end_of_run']})")
    for name in ("A", "B"):
        p = s[name]; r = p["requests"]; v = p["verification"]
        out(f"[{name}] caps physiological {p['caps']['physiological']} / tracking {p['caps']['tracking']} · requests expected {r['expected']} accepted {r['accepted']} failed {r['failed']} timeout {r['timeout']} unsent {r['unsent']} omitted {r['omitted_by_scenario']}"
            f" · write latency p50 {f_ms(p['latency_s']['p50'])} p95 {f_ms(p['latency_s']['p95'])} max {f_ms(p['latency_s']['max'])} · lateness p50 {f_ms(p['scheduling_lateness_s']['p50'])} p95 {f_ms(p['scheduling_lateness_s']['p95'])} max {f_ms(p['scheduling_lateness_s']['max'])}"
            f" · reads ok {p['reads']['ok']}/{p['reads']['expected']} (read latency p50 {f_ms(p['read_latency_s'].get('p50'))})")
        for stream, sp in p["retained_spans"].items():
            out(f"      {stream:14s} retained per athlete {sp['rows_min']}-{sp['rows_max']} rows spanning {sp['span_s_min']}-{sp['span_s_max']} s (cap {p['caps'][stream]}: a count, not a duration)")
        ov_bad = [k for k, val in p["overview_checks"].items() if val is not True]
        out(f"      verification {v['passed']} passed / {v['failed']} failed / {v['unknown']} unknown of {v['checked']} · overview checks {'all passed' if not ov_bad else 'not passed: ' + ', '.join(ov_bad)} · effort checks {p['effort_overall']['passed']} passed / {p['effort_overall']['failed']} failed / {p['effort_overall']['unknown']} unknown"
            f" · export {p['exported']} replayed {p['replayed']} checks {'all passed' if all(x is True for x in p['export_checks'].values()) else p['export_checks']}")
    out("Effort review, per athlete (A: default caps · B: larger tracking cap):")
    for row in cmp["per_athlete"]:
        a, b = row["A"], row["B"]
        fa = "null distance" if a["distance_m"] is None else f"{a['distance_m']:.2f} m"
        fb = "null distance" if b["distance_m"] is None else f"{b['distance_m']:.2f} m"
        hsb = "n/a" if b["high_speed_s"] is None else f"{b['high_speed_s']:.2f} s"
        aa = a.get("availability") or {}
        out(f"  {row['athlete_id']}: A {a['tracking_samples']} tracking samples, {fa} (retained {aa.get('retained_count')} at cap {aa.get('at_cap')}, selected {aa.get('selected_count')}) · B {b['tracking_samples']} samples, {fb}, high-speed {hsb} · heart-rate periods agree: {row['heart_rate_periods_agree']} · difference explained by the caps: {row['difference_explained_by_caps']}")
    out(f"Athletes with a null distance under A and a value under B: {cmp['athletes_with_null_distance_under_A_and_a_value_under_B']} of {len(cmp['per_athlete'])} · heart-rate periods agree for all: {cmp['heart_rate_periods_agree_for_all']}")
    m = report["machine"]
    out(f"Machine: {m['platform']} · Python {m['python']} · {m['cpu_count']} CPUs. Observations only: no threshold, not a capacity claim.")


def all_passed(report: dict) -> bool:
    for name in ("A", "B"):
        s = report["summary"][name]
        if s["verification"]["failed"] or s["verification"]["unknown"] or s["effort_overall"]["failed"] or s["effort_overall"]["unknown"]:
            return False
        if not all(x is True for x in s["overview_checks"].values()) or not all(x is True for x in s["export_checks"].values()) or s["reads"]["checks_failed"] or s["reads"]["unsent"]:
            return False
        if s["requests"]["unsent"]:
            return False
    return report["comparison"]["heart_rate_periods_agree_for_all"] and report["config"]["effort"]["outside_default_tracking_cap_at_end_of_run"]


# ---- command line ----------------------------------------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--athletes", type=int, default=DEFAULT_ATHLETES, help=f"fictional athletes (default {DEFAULT_ATHLETES})")
    p.add_argument("--duration", type=float, default=DEFAULT_DURATION_S, help=f"seconds of generation per run (default {DEFAULT_DURATION_S:g})")
    p.add_argument("--physiological-rate", type=float, default=DEFAULT_PHYSIOLOGICAL_RATE_HZ, help=f"physiological samples per second per athlete (default {DEFAULT_PHYSIOLOGICAL_RATE_HZ:g})")
    p.add_argument("--tracking-rate", type=float, default=DEFAULT_TRACKING_RATE_HZ, help=f"tracking samples per second per athlete (default {DEFAULT_TRACKING_RATE_HZ:g})")
    p.add_argument("--physiological-cap", type=int, default=DEFAULT_RETENTION_CAP, help=f"profile B physiological cap (default {DEFAULT_RETENTION_CAP}); profile A always uses the backend defaults")
    p.add_argument("--tracking-cap", type=int, default=DEFAULT_LARGER_TRACKING_CAP, help=f"profile B tracking cap (default {DEFAULT_LARGER_TRACKING_CAP}: about five minutes only at 10 Hz)")
    p.add_argument("--effort-start", type=int, default=DEFAULT_EFFORT[0], help=f"effort start, whole seconds after the run start (default {DEFAULT_EFFORT[0]})")
    p.add_argument("--effort-end", type=int, default=DEFAULT_EFFORT[1], help=f"effort end, whole seconds after the run start (default {DEFAULT_EFFORT[1]}); must lie outside the default tracking cap by the end of the run")
    p.add_argument("--read-interval", type=float, default=2.0, help="seconds between overview reads during each run; 0 disables them (default 2)")
    p.add_argument("--timeout", type=float, default=5.0, help="per-request timeout in seconds (default 5)")
    p.add_argument("--workers", type=int, default=8, help="concurrent request workers (default 8)")
    p.add_argument("--report", type=Path, default=None, help="JSON report path (default reports/retention-compare-<UTC time>.json, ignored by git)")
    return p


def main(argv: list[str] | None = None, *, out: Callable[[str], None] = print, backend_factory: Callable = IsolatedBackend) -> int:
    args = build_parser().parse_args(argv)
    if args.athletes < 1 or args.duration <= 0 or args.physiological_rate <= 0 or args.tracking_rate <= 0 or args.timeout <= 0 or args.physiological_cap < 1 or args.tracking_cap < 1:
        out("athletes and caps must be positive integers; duration, rates and timeout must be positive")
        return 2
    effort = Effort(args.effort_start, args.effort_end)
    if effort.start_s < 0 or not (1 <= effort.duration_s <= 120):
        out("the effort must start at or after the run start and last 1 to 120 whole seconds")
        return 2
    cfg = RunConfig(athletes=args.athletes, duration_s=args.duration, rate_hz=1.0, physiological_rate_hz=args.physiological_rate, tracking_rate_hz=args.tracking_rate,
                    timeout_s=args.timeout, workers=args.workers, read_interval_s=max(0.0, args.read_interval))
    if not effort_outside_default_cap(cfg, effort):
        total = int(round(cfg.duration_s * cfg.rate_for("tracking")))
        out(f"the effort must end before the oldest tracking sample that the default cap of {DEFAULT_RETENTION_CAP} keeps at the end of the run ({max(0, total - DEFAULT_RETENTION_CAP) / cfg.rate_for('tracking'):g} s after start); choose an earlier effort or a longer run")
        return 2
    if effort.end_s + AFTER_S > cfg.duration_s:
        out(f"note: the after period ends {effort.end_s + AFTER_S:g} s after start, beyond the {cfg.duration_s:g} s run; its later part will simply hold no samples")
    profiles = {"A": {"physiological": DEFAULT_RETENTION_CAP, "tracking": DEFAULT_RETENTION_CAP}, "B": {"physiological": args.physiological_cap, "tracking": args.tracking_cap}}
    report_path = args.report or (ROOT / "reports" / f"retention-compare-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json")
    results: dict = {}
    interrupted = {"flag": False}

    def on_signal(signum, _frame):
        interrupted["flag"] = True
        raise KeyboardInterrupt

    previous = {s: signal.signal(s, on_signal) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        for name, caps in profiles.items():
            results[name] = run_profile(name, caps, cfg, effort, out, backend_factory)   # each run cleans up its own backend, interrupted or not
            print_summary(results[name]["report"], lambda s: out(f"[{name}] {s}"))
        report = build_comparison_report(cfg, effort, results["A"], results["B"])
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print_comparison(report, out)
        out(f"JSON report: {report_path}")
        return 0 if all_passed(report) else 1
    except KeyboardInterrupt:
        out("interrupted: the isolated backends started here were stopped and their temporary folders removed")
        return 130
    finally:
        for s, h in previous.items():
            signal.signal(s, h)


if __name__ == "__main__":
    sys.exit(main())
