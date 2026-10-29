"""scripts/validate_team_scale.py: scheduling, counting, verification and cleanup, with a fake clock and a mocked backend.

No real backend process or real sleeping is involved; the end-to-end run is exercised against the app through the
TestClient on an isolated database. All athletes and values are fictional.
"""

import importlib.util
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


vt = load("validate_team_scale")
START = datetime(2026, 10, 20, 10, 0, 0, tzinfo=UTC)


class FakeClock:
    """Monotonic clock advanced by sleep() and by a per-request cost."""

    def __init__(self, request_cost=0.001):
        self.now = 100.0
        self.request_cost = request_cost
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.slept.append(s)
        self.now += s


class Borrowed:
    def __init__(self, client):
        self.client = client

    def close(self):
        pass

    def post(self, url, json=None):
        return self.client.post(url.replace("http://test", ""), json=json)


def mock_client(handler, clock=None, cost=0.0):
    def _handler(request):
        if clock is not None:
            clock.now += cost
        return handler(request)
    return httpx.Client(transport=httpx.MockTransport(_handler), base_url="http://test")


# ---- fictional samples ------------------------------------------------------------------------------------------

def test_sample_payloads_are_deterministic_distinct_per_athlete_and_explicitly_simulated():
    a = vt.sample_payloads(0, 3, 15.0, 1.0, START)
    b = vt.sample_payloads(1, 3, 15.0, 1.0, START)
    assert a == vt.sample_payloads(0, 3, 15.0, 1.0, START)
    assert a["physiological"]["athlete_id"] == "fictional-athlete-01" and b["physiological"]["athlete_id"] == "fictional-athlete-02"
    assert a["physiological"]["device_id"] == "fictional-wearable-01" and a["tracking"]["device_id"] == "fictional-tracker-01"
    assert a["physiological"]["source"] == "simulated" and a["tracking"]["source"] == "simulated"
    assert a["physiological"]["heart_rate_bpm"] != b["physiological"]["heart_rate_bpm"]
    assert a["tracking"]["timestamp"] == START.isoformat() and a["tracking"]["tracking_confidence"] == 1.0


# ---- scheduling and counting -----------------------------------------------------------------------------------------

def test_run_load_schedules_on_the_monotonic_clock_and_records_lateness_and_latency():
    clock = FakeClock()
    cfg = vt.RunConfig(athletes=2, duration_s=3, rate_hz=1.0, workers=1)
    client = mock_client(lambda req: httpx.Response(200, json={}), clock, cost=0.01)
    res = vt.run_load("http://test", cfg, make_client=lambda: client, clock=clock, sleep=clock.sleep, start_utc=START)
    records, actual, start = res.records, res.actual_s, res.start_utc
    assert len(records) == 2 * 3 * 2 and all(r.outcome == "accepted" and r.status == 200 for r in records)
    assert sorted({r.tick for r in records}) == [0, 1, 2]
    assert all(abs(r.latency_s - 0.01) < 1e-9 for r in records), "latency is the fake request cost"
    assert all(r.lateness_s is not None and r.lateness_s >= 0 for r in records)
    # the first tick is due at once; later ticks wait for their due time (sleep is only called with positive waits)
    assert all(s > 0 for s in clock.slept)
    assert actual >= 2.0


def test_failures_are_counted_by_kind_never_retried_and_never_invented():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if "tracking" in str(req.url):
            return httpx.Response(422, json={"detail": "bad"})
        if calls["n"] % 3 == 0:
            raise httpx.ReadTimeout("slow")
        return httpx.Response(200, json={})
    clock = FakeClock()
    cfg = vt.RunConfig(athletes=3, duration_s=2, rate_hz=1.0, workers=1, read_interval_s=0)  # no overview reads: this test counts sample attempts only
    res = vt.run_load("http://test", cfg, make_client=lambda: mock_client(handler), clock=clock, sleep=clock.sleep, start_utc=START)
    records, actual, start = res.records, res.actual_s, res.start_utc
    counts = vt.count_outcomes(records)
    assert counts["expected"] == 12 and counts["attempted"] == 12 and counts["unsent"] == 0
    assert counts["by_stream"]["tracking"]["failed"] == 6 and counts["http_status_counts"] == {"422": 6}
    assert counts["timeout"] + counts["by_stream"]["physiological"]["accepted"] == 6 and counts["timeout"] >= 1
    assert calls["n"] == 12, "exactly one attempt per request: nothing is retried"
    assert all(r.detail.endswith("acceptance unknown") for r in records if r.outcome == "timeout")


def test_requests_after_the_deadline_are_unsent_not_successes():
    clock = FakeClock()
    cfg = vt.RunConfig(athletes=1, duration_s=2, rate_hz=1.0, workers=1, grace_s=0.0)

    def handler(req):
        clock.now += 5.0  # each request takes longer than the whole run: later ones miss the deadline
        return httpx.Response(200, json={})
    res = vt.run_load("http://test", cfg, make_client=lambda: mock_client(handler), clock=clock, sleep=clock.sleep, start_utc=START)
    records, actual, start = res.records, res.actual_s, res.start_utc
    counts = vt.count_outcomes(records)
    assert counts["expected"] == 4 and counts["unsent"] >= 1 and counts["accepted"] + counts["unsent"] == 4
    assert all(r.sent_s is None and "deadline" in r.detail or r.outcome == "accepted" for r in records)


def test_lateness_is_reported_when_the_rate_cannot_be_kept():
    clock = FakeClock()
    cfg = vt.RunConfig(athletes=1, duration_s=3, rate_hz=1.0, workers=1)

    def handler(req):
        clock.now += 0.6  # two requests per tick at 0.6 s each: 1.2 s per tick against a 1 s schedule
        return httpx.Response(200, json={})
    res = vt.run_load("http://test", cfg, make_client=lambda: mock_client(handler), clock=clock, sleep=clock.sleep, start_utc=START)
    records, actual, start = res.records, res.actual_s, res.start_utc
    late = [r.lateness_s for r in records if r.sent_s is not None]
    assert len(late) == 6 and max(late) >= 0.6 and actual > 3.0, "the slip is visible in lateness and in the actual duration"
    assert vt.count_outcomes(records)["accepted"] == 6


def test_percentiles_and_summary():
    assert vt.percentile([], 50) is None
    assert vt.percentile([5, 1, 3, 2, 4], 50) == 3 and vt.percentile([5, 1, 3, 2, 4], 95) == 5 and vt.percentile([7], 95) == 7
    s = vt.summarize([0.1, 0.2, 0.3])
    assert s["count"] == 3 and s["max"] == 0.3 and abs(s["mean"] - 0.2) < 1e-12


def test_expected_retained_applies_the_cap_and_only_accepted_samples():
    recs = [vt.RequestRecord("a", "physiological", t, t, t, 0.001, "accepted" if t % 2 == 0 else "http_error", 200, "", {"timestamp": f"2026-10-20T10:{t // 60:02d}:{t % 60:02d}Z"}) for t in range(700)]
    kept = vt.expected_retained(recs, "a", "physiological")
    assert len(kept) == vt.RETENTION_CAP and kept[-1].tick == 698 and all(r.outcome == "accepted" for r in kept)
    assert vt.expected_retained(recs, "b", "physiological") == []


# ---- end to end against the app on an isolated database ---------------------------------------------------------------

def test_end_to_end_against_the_app_and_verification_passes(client, tmp_path):
    clock = FakeClock()
    cfg = vt.RunConfig(athletes=3, duration_s=4, rate_hz=1.0, workers=2)
    start = datetime(2026, 10, 25, 11, 59, 0, tzinfo=UTC)  # in the past relative to the backend clock, so ingestion accepts it
    res = vt.run_load("http://test", cfg, make_client=lambda: Borrowed(client), clock=clock, sleep=clock.sleep, start_utc=start)
    records, actual, start_utc = res.records, res.actual_s, res.start_utc
    counts = vt.count_outcomes(records)
    assert counts["accepted"] == 3 * 4 * 2 and counts["failed"] == 0 and counts["unsent"] == 0
    verification = vt.verify(Borrowed2(client), "", records, cfg, start_utc)
    assert verification["overall"] == {"checked": 3, "passed": 3, "failed": 0, "unknown": 0, "device_ids_distinct": True, "athletes_distinct": True}
    for a in verification["per_athlete"]:
        assert a["status"] == "passed" and all(v is True for v in a["checks"].values()), a
    report = vt.build_report(cfg, {"base_url": "http://test", "pid": 0, "db_path": "x", "command": [], "retention_cap": 300}, records, actual, start_utc, verification)
    text = json.dumps(report, default=str)
    assert report["requests"]["accepted"] == 24 and report["latency_s"]["count"] == 24 and report["fictional_data"] is True
    assert "not a capacity" in text.lower() or "production capacity" in text.lower()
    lines = []
    vt.print_summary(report, lines.append)
    assert any("Verification: 3 passed · 0 failed · 0 unknown of 3 athletes" in l for l in lines)
    assert not any("pass threshold" in l.lower() for l in lines)


class Borrowed2:
    """GET side of the verification against the TestClient (params are passed through)."""

    def __init__(self, client):
        self.client = client

    def get(self, url, params=None):
        return self.client.get(url, params=params)


def test_verification_reports_unknown_for_timed_out_athletes_and_failed_for_mismatches(client):
    clock = FakeClock()
    cfg = vt.RunConfig(athletes=2, duration_s=2, rate_hz=1.0, workers=1)
    start = datetime(2026, 10, 25, 11, 59, 0, tzinfo=UTC)
    res = vt.run_load("http://test", cfg, make_client=lambda: Borrowed(client), clock=clock, sleep=clock.sleep, start_utc=start)
    records, actual, start_utc = res.records, res.actual_s, res.start_utc
    # pretend athlete 1's second tracking request timed out: the backend did store it, so the count differs from "accepted" -> unknown, not failed
    rec = next(r for r in records if r.athlete == "fictional-athlete-01" and r.stream == "tracking" and r.tick == 1)
    rec.outcome, rec.status, rec.detail = "timeout", None, "ReadTimeout: acceptance unknown"
    v = vt.verify(Borrowed2(client), "", records, cfg, start_utc)
    a1 = next(p for p in v["per_athlete"] if p["athlete_id"] == "fictional-athlete-01")
    assert a1["status"] == "unknown" and a1["checks"]["tracking_retained_count"] is None and v["overall"]["unknown"] == 1
    # a sample the backend never received but which the records call accepted is a real mismatch -> failed
    recs2 = list(records)
    recs2.append(vt.RequestRecord("fictional-athlete-02", "physiological", 9, 9.0, 9.0, 0.001, "accepted", 200, "", {"athlete_id": "fictional-athlete-02", "timestamp": "2026-10-25T11:59:30+00:00", "heart_rate_bpm": 100}))
    v2 = vt.verify(Borrowed2(client), "", recs2, cfg, start_utc)
    a2 = next(p for p in v2["per_athlete"] if p["athlete_id"] == "fictional-athlete-02")
    assert a2["status"] == "failed" and a2["checks"]["physiological_retained_count"] is False


# ---- isolated backend process handling -------------------------------------------------------------------------------

def test_free_port_is_a_usable_localhost_port():
    import socket
    p = vt.free_port()
    assert 1024 < p < 65536
    with socket.socket() as s:
        s.bind(("127.0.0.1", p))


def test_backend_stop_terminates_only_its_own_process(monkeypatch):
    events = []

    class FakeProc:
        pid = 4242

        def __init__(self):
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            events.append("terminate"); self.returncode = -15

        def wait(self, timeout=None):
            events.append("wait"); return self.returncode

        def kill(self):
            events.append("kill")
    b = vt.IsolatedBackend(port=65000)
    b.proc = FakeProc()
    b.stop()
    assert events == ["terminate", "wait"] and b.proc is None
    b.stop()  # idempotent: nothing else is touched
    assert events == ["terminate", "wait"]
    b.cleanup()


def test_backend_start_reports_an_early_exit(monkeypatch):
    class DeadProc:
        pid = 1
        returncode = 3

        def poll(self):
            return 3

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 3
    monkeypatch.setattr(vt.subprocess, "Popen", lambda *a, **k: DeadProc())
    b = vt.IsolatedBackend(port=65001)
    with pytest.raises(RuntimeError, match="exited early with code 3"):
        b.start(ready_timeout_s=1.0)
    b.cleanup()


def test_main_rejects_bad_arguments(capsys):
    assert vt.main(["--athletes", "0"], out=print) == 2
    assert vt.main(["--duration", "-1"], out=print) == 2


# ---- overview reads and the interrupted scenario -----------------------------------------------------------------------

def test_scenario_plan_is_deterministic_and_small():
    plan = vt.scenario_plan("interrupted", 20, vt.RunConfig(athletes=20, duration_s=30))
    assert plan == vt.scenario_plan("interrupted", 20, vt.RunConfig(athletes=20, duration_s=30))
    assert sorted(plan.omitted) == [(i, t, "tracking") for i in (0, 1) for t in range(5, 10)]
    assert plan.late == {(2, t, "physiological"): 12 for t in range(5, 10)}
    assert plan.deliveries_at(12) == [(2, 9, "physiological"), (2, 8, "physiological"), (2, 7, "physiological"), (2, 6, "physiological"), (2, 5, "physiological")], "newest first, so the older ones arrive after newer ones"
    assert sorted(plan.statuses) == [0, 10, 12] and plan.statuses[12] == plan.statuses[0]
    assert vt.scenario_plan("healthy", 20, vt.RunConfig(athletes=20, duration_s=30)) == vt.Plan()
    with pytest.raises(ValueError):
        vt.scenario_plan("interrupted", 2, vt.RunConfig(athletes=2, duration_s=30))
    with pytest.raises(ValueError):
        vt.scenario_plan("interrupted", 3, vt.RunConfig(athletes=3, duration_s=12))
    with pytest.raises(ValueError):
        vt.scenario_plan("chaos", 20, vt.RunConfig(athletes=20, duration_s=30))


def test_scenario_accounting_distinguishes_omitted_from_unsent_and_keeps_late_timestamps():
    clock = FakeClock()
    cfg = vt.RunConfig(athletes=3, duration_s=13, rate_hz=1.0, workers=1, read_interval_s=2.0)
    seen = []

    def handler(req):
        seen.append((str(req.url.path), req.content))
        if req.url.path.endswith("/live"):
            return httpx.Response(200, json={"team_id": "fictional-team", "captured_at": START.isoformat(), "athletes": [{"athlete": {"id": vt.athlete_id(i), "name": "x", "jersey_number": i, "sport": "f", "team_id": "fictional-team"}, "physiological": None, "tracking": None, "physiological_age_seconds": None, "tracking_age_seconds": None, "devices": []} for i in range(3)]})
        return httpx.Response(200, json={})
    res = vt.run_load("http://test", cfg, make_client=lambda: mock_client(handler), clock=clock, sleep=clock.sleep, start_utc=START, plan=vt.scenario_plan("interrupted", 3, vt.RunConfig(athletes=3, duration_s=13)))
    counts = vt.count_outcomes(res.records)
    assert counts["expected"] == 3 * 13 * 2 and counts["omitted_by_scenario"] == 10 and counts["unsent"] == 0
    assert counts["attempted"] == counts["expected"] - 10 and counts["accepted"] == counts["attempted"] and counts["delivered_late"] == 5
    omitted = [r for r in res.records if r.outcome == "omitted"]
    assert all(r.sent_s is None and r.plan == "omitted" and r.stream == "tracking" and r.athlete in ("fictional-athlete-01", "fictional-athlete-02") for r in omitted)
    late = [r for r in res.records if r.plan == "late"]
    assert len(late) == 5 and all(r.athlete == "fictional-athlete-03" and r.stream == "physiological" for r in late)
    assert all(r.payload["timestamp"] == (START + timedelta(seconds=r.tick)).isoformat() for r in late), "a late delivery keeps its own tick's timestamp"
    assert all(abs(r.scheduled_s - 12.0) < 1e-9 for r in late), "but is due at the delivery tick"
    assert [x.payload["battery_percent"] for x in sorted(res.statuses, key=lambda x: (x.tick, x.athlete))] == [90.0] * 3 + [80.0] * 3 + [90.0] * 3
    assert all(x.outcome == "accepted" for x in res.statuses)
    assert [r.scheduled_s for r in res.reads] == [0, 2, 4, 6, 8, 10, 12] and all(r.outcome == "ok" for r in res.reads)
    # the mocked overview never shows samples, so during the pause the in-progress check must FAIL (nothing is current)
    assert [r.checks.get("paused_tracking_stale_while_physiological_current") for r in res.reads] == [None, None, None, False, False, None, None]
    counts_r = vt.count_reads(res.reads)
    assert counts_r["checks_failed"] == {"paused_tracking_stale_while_physiological_current": 2} and counts_r["checks_evaluated"]["paused_tracking_stale_while_physiological_current"] == 2
    assert not any(path.endswith("/live") and b"fictional-athlete" in body for path, body in seen), "reads never send samples"


def test_check_overview_detects_substitution_missing_athletes_negative_ages_and_backwards_timestamps():
    ids = ["fictional-athlete-01", "fictional-athlete-02"]
    row = lambda aid, sid, ts, age: {"athlete": {"id": aid}, "physiological": {"athlete_id": sid, "timestamp": ts, "device_id": "w"}, "tracking": None, "physiological_age_seconds": age, "tracking_age_seconds": None, "devices": [{"device_id": "w"}]}  # noqa: E731
    prev = {}
    ok = vt.check_overview({"athletes": [row(ids[0], ids[0], "2026-10-20T10:00:05Z", 1.0), row(ids[1], ids[1], "2026-10-20T10:00:05Z", 0.0)]}, ids, prev)
    assert all(ok.values()), ok
    bad = vt.check_overview({"athletes": [row(ids[0], ids[1], "2026-10-20T10:00:04Z", -1.0), {"athlete": {"id": "stranger"}, "physiological": None, "tracking": None, "physiological_age_seconds": None, "tracking_age_seconds": None, "devices": [{"device_id": "ghost"}]}]}, ids, prev)
    assert bad["one_entry_per_registered_athlete"] is False and bad["samples_name_their_own_athlete"] is False and bad["ages_never_negative"] is False
    assert bad["latest_timestamps_never_go_backwards"] is False, "10:00:04 after 10:00:05 for athlete 1"
    assert bad["device_ids_match_samples"] is False


def test_pause_in_progress_check_only_applies_during_the_pause_and_sees_stale_tracking_with_current_physiological():
    plan = vt.scenario_plan("interrupted", 3, vt.RunConfig(athletes=3, duration_s=13))
    row = lambda i, trk_tick, phys_tick: {"athlete": {"id": vt.athlete_id(i)}, "tracking": {"timestamp": (START + timedelta(seconds=trk_tick)).isoformat()}, "physiological": {"timestamp": (START + timedelta(seconds=phys_tick)).isoformat()}}  # noqa: E731
    good = {"athletes": [row(0, 4, 7), row(1, 4, 8), row(2, 8, 8)]}
    assert vt.check_pause_in_progress(good, plan, 8, START, 1.0) is True
    assert vt.check_pause_in_progress(good, plan, 4, START, 1.0) is None, "before the pause: not decidable"
    assert vt.check_pause_in_progress(good, plan, 5, START, 1.0) is None, "the first paused tick: the previous tracking tick may still be current"
    assert vt.check_pause_in_progress(good, plan, 10, START, 1.0) is None, "after the pause"
    leaked = {"athletes": [row(0, 7, 7), row(1, 4, 8), row(2, 8, 8)]}
    assert vt.check_pause_in_progress(leaked, plan, 8, START, 1.0) is False, "a tracking reading from inside the pause means the pause was not reflected"
    stale_phys = {"athletes": [row(0, 4, 3), row(1, 4, 8), row(2, 8, 8)]}
    assert vt.check_pause_in_progress(stale_phys, plan, 8, START, 1.0) is False, "the other stream must stay current"
    assert vt.check_pause_in_progress({"athletes": []}, plan, 8, START, 1.0) is False
    assert vt.check_pause_in_progress(good, vt.Plan(), 8, START, 1.0) is None, "no pause planned"


def test_overview_checks_depending_on_timed_out_requests_are_unknown_not_passed(client):
    clock = FakeClock()
    cfg = vt.RunConfig(athletes=3, duration_s=13, rate_hz=1.0, workers=1, read_interval_s=0)
    start = datetime(2026, 10, 25, 11, 58, 0, tzinfo=UTC)
    vt.register_team("", cfg, out=lambda s: None, client=client, clock=clock)
    plan = vt.scenario_plan("interrupted", 3, vt.RunConfig(athletes=3, duration_s=13))
    res = vt.run_load("http://test", cfg, make_client=lambda: BorrowedAll(client), clock=clock, sleep=clock.sleep, start_utc=start, plan=plan)
    # pretend the late athlete's newest on-time sample and one paused athlete's physiological sample timed out (they were in fact stored)
    for r in res.records:
        if (r.athlete == "fictional-athlete-03" and r.stream == "physiological" and r.tick == 12) or (r.athlete == "fictional-athlete-01" and r.stream == "physiological" and r.tick == 7):
            r.outcome, r.status, r.detail = "timeout", None, "ReadTimeout: acceptance unknown"
    for x in res.statuses:
        if x.athlete == "fictional-athlete-02" and x.tick == 10:
            x.outcome = "timeout"
    ov = vt.verify_overview(Borrowed2(client), "", res.records, res.statuses, cfg, plan)
    assert ov["checks"]["latest_is_newest_accepted_timestamp"] is None, "the stored tick-12 sample does not match 'accepted' but the request timed out: unknown"
    assert ov["checks"]["older_late_arrival_did_not_replace_newer_latest"] is None
    assert ov["checks"]["paused_stream_absent_while_other_stream_continued"] is None
    assert ov["checks"]["device_status_is_newest_report_old_resend_ignored"] is None
    assert any("could not be settled" in n for n in ov["notes"])
    report = vt.build_report(cfg, {"base_url": "x", "pid": 0, "db_path": "x", "command": [], "retention_cap": 300}, res.records, res.actual_s, res.start_utc, vt.verify(Borrowed2(client), "", res.records, cfg, res.start_utc), reads=res.reads, statuses=res.statuses, overview_verification=ov, plan=plan)
    lines = []
    vt.print_summary(report, lines.append)
    assert any("Overview verification: UNKNOWN (outcome depends on timed-out requests)" in l for l in lines)
    assert not any("all checks passed" in l for l in lines)


def test_interrupted_scenario_end_to_end_against_the_app(client):
    clock = FakeClock()
    cfg = vt.RunConfig(athletes=3, duration_s=13, rate_hz=1.0, workers=1, read_interval_s=2.0)
    start = datetime(2026, 10, 25, 11, 58, 0, tzinfo=UTC)
    setup = vt.register_team("", cfg, out=lambda s: None, client=client, clock=clock)
    assert setup["registered"] == 3 and setup["failed"] == 0
    plan = vt.scenario_plan("interrupted", 3, vt.RunConfig(athletes=3, duration_s=13))
    res = vt.run_load("http://test", cfg, make_client=lambda: BorrowedAll(client), clock=clock, sleep=clock.sleep, start_utc=start, plan=plan)
    counts = vt.count_outcomes(res.records)
    assert counts["failed"] == 0 and counts["unsent"] == 0 and counts["omitted_by_scenario"] == 10 and counts["delivered_late"] == 5
    assert all(r.outcome == "ok" and all(v is True for v in r.checks.values()) for r in res.reads), [r.checks for r in res.reads]
    in_pause = {r.scheduled_s: r.checks.get("paused_tracking_stale_while_physiological_current") for r in res.reads}
    assert in_pause == {0: None, 2: None, 4: None, 6: True, 8: True, 10: None, 12: None}, "the pause was visible in the live overview while it was happening"
    assert vt.count_reads(res.reads)["checks_evaluated"]["paused_tracking_stale_while_physiological_current"] == 2
    v = vt.verify(Borrowed2(client), "", res.records, cfg, res.start_utc)
    assert v["overall"]["failed"] == 0 and v["overall"]["unknown"] == 0
    ov = vt.verify_overview(Borrowed2(client), "", res.records, res.statuses, cfg, plan)
    assert ov["checks"] == {"one_entry_per_registered_athlete": True, "samples_name_their_own_athlete": True, "ages_never_negative": True, "latest_timestamps_never_go_backwards": True, "device_ids_match_samples": True,
                            "latest_is_newest_accepted_timestamp": True, "older_late_arrival_did_not_replace_newer_latest": True, "paused_stream_absent_while_other_stream_continued": True, "device_status_is_newest_report_old_resend_ignored": True}, ov
    # the late athlete's latest physiological reading is tick 12, not the tick-9 sample that arrived last
    latest = client.get("/api/v1/athletes/fictional-athlete-03/physiological/latest").json()
    assert latest["timestamp"] == (start + timedelta(seconds=12)).isoformat().replace("+00:00", "Z")
    status = client.get("/api/v1/devices/fictional-wearable-01/status/latest").json()
    assert status["battery_percent"] == 80.0 and status["timestamp"].startswith((start + timedelta(seconds=10)).isoformat()[:19])
    report = vt.build_report(cfg, {"base_url": "x", "pid": 0, "db_path": "x", "command": [], "retention_cap": 300}, res.records, res.actual_s, res.start_utc, v, reads=res.reads, statuses=res.statuses, setup=setup, overview_verification=ov, plan=plan)
    assert report["scenario"]["omitted_samples"] == 10 and report["requests"]["omitted_by_scenario"] == 10 and report["reads"]["ok"] == 7 and report["device_status_reports"]["accepted"] == 9
    lines = []
    vt.print_summary(report, lines.append)
    assert any("Scenario interrupted: 10 samples left out on purpose" in l for l in lines) and any("Overview verification: all checks passed" in l for l in lines)


class BorrowedAll:
    """POST and GET against the TestClient for the run (samples, statuses and overview reads)."""

    def __init__(self, client):
        self.client = client

    def close(self):
        pass

    def post(self, url, json=None):
        return self.client.post(url.replace("http://test", ""), json=json)

    def get(self, url, params=None):
        return self.client.get(url.replace("http://test", ""), params=params)


# ---- different physiological and tracking rates --------------------------------------------------------------------------

def test_rate_precedence_per_stream_option_overrides_rate():
    cfg = vt.RunConfig(rate_hz=2.0)
    assert cfg.rate_for("physiological") == 2.0 and cfg.rate_for("tracking") == 2.0
    cfg = vt.RunConfig(rate_hz=2.0, tracking_rate_hz=10.0)
    assert cfg.rate_for("physiological") == 2.0 and cfg.rate_for("tracking") == 10.0
    cfg = vt.RunConfig(duration_s=4, physiological_rate_hz=1.0, tracking_rate_hz=5.0, athletes=3)
    assert vt.stream_ticks(cfg, "physiological") == 4 and vt.stream_ticks(cfg, "tracking") == 20 and cfg.expected_samples == 3 * (4 + 20)
    args = vt.build_parser().parse_args(["--rate", "2", "--tracking-rate", "10"])
    assert (args.rate, args.physiological_rate, args.tracking_rate) == (2.0, None, 10.0)


def test_unequal_rates_schedule_each_stream_on_its_own_grid_without_duplicating_measurements():
    clock = FakeClock()
    cfg = vt.RunConfig(athletes=2, duration_s=3, physiological_rate_hz=1.0, tracking_rate_hz=5.0, workers=1, read_interval_s=0)
    res = vt.run_load("http://test", cfg, make_client=lambda: mock_client(lambda req: httpx.Response(200, json={})), clock=clock, sleep=clock.sleep, start_utc=START)
    phys = [r for r in res.records if r.stream == "physiological"]; trk = [r for r in res.records if r.stream == "tracking"]
    assert len(phys) == 2 * 3 and len(trk) == 2 * 15 and all(r.outcome == "accepted" for r in res.records)
    assert sorted({r.payload["timestamp"] for r in phys}) == [(START + timedelta(seconds=t)).isoformat() for t in range(3)], "physiological samples sit on the 1 Hz grid only"
    assert sorted({r.payload["timestamp"] for r in trk}) == [(START + timedelta(seconds=t / 5)).isoformat() for t in range(15)], "tracking samples sit on the 5 Hz grid"
    assert all("heart_rate_bpm" not in r.payload for r in trk) and all("speed_mps" not in r.payload for r in phys), "no measurement is duplicated onto the other stream's grid"
    assert sorted({round(r.scheduled_s, 3) for r in trk}) == [round(t / 5, 3) for t in range(15)]
    rp, rt = vt.stream_report(res.records, "physiological", 1.0, 2), vt.stream_report(res.records, "tracking", 5.0, 2)
    assert rp["expected"] == 6 and rp["measurement_interval_s"] == 3.0 and abs(rp["delivered_rate_hz"] - 1.0) < 1e-9
    assert rt["expected"] == 30 and abs(rt["measurement_interval_s"] - 3.0) < 1e-9 and abs(rt["delivered_rate_hz"] - 5.0) < 1e-9
    assert rp["unknown_timeout"] == 0 and rp["omitted_by_scenario"] == 0 and rp["unsent"] == 0


def test_interrupted_scenario_maps_onto_each_streams_grid_and_reads_keep_their_own_cadence():
    cfg = vt.RunConfig(athletes=3, duration_s=13, physiological_rate_hz=1.0, tracking_rate_hz=10.0, workers=1, read_interval_s=2.0)
    plan = vt.scenario_plan("interrupted", 3, cfg)
    assert sorted({t for (i, t, s) in plan.omitted}) == list(range(50, 100)) and len(plan.omitted) == 100, "the 5-10 s pause is 50 tracking ticks at 10 Hz"
    assert plan.late == {(2, t, "physiological"): 12 for t in range(5, 10)}
    clock = FakeClock()
    body = {"team_id": "fictional-team", "captured_at": START.isoformat(), "athletes": []}
    res = vt.run_load("http://test", cfg, make_client=lambda: mock_client(lambda req: httpx.Response(200, json=body if str(req.url).endswith("/live") else {})), clock=clock, sleep=clock.sleep, start_utc=START, plan=plan)
    counts = vt.count_outcomes(res.records)
    assert counts["expected"] == 3 * (13 + 130) and counts["omitted_by_scenario"] == 100 and counts["delivered_late"] == 5 and counts["unsent"] == 0
    assert [r.scheduled_s for r in res.reads] == [0, 2, 4, 6, 8, 10, 12], "overview reads keep their 2 s cadence whatever the sample rates"
    assert [x.tick for x in sorted(res.statuses, key=lambda x: x.tick)][::3] == [0, 10, 12], "device statuses keep their own cadence in seconds"
    late = [r for r in res.records if r.plan == "late"]
    assert all(abs(r.scheduled_s - 12.0) < 1e-9 and r.payload["timestamp"] == (START + timedelta(seconds=r.tick)).isoformat() for r in late)


def test_unequal_rates_end_to_end_with_window_and_summary_on_each_grid(client):
    clock = FakeClock()
    cfg = vt.RunConfig(athletes=2, duration_s=3, physiological_rate_hz=1.0, tracking_rate_hz=10.0, workers=1, read_interval_s=0)
    start = datetime(2026, 10, 25, 11, 58, 0, tzinfo=UTC)
    vt.register_team("", cfg, out=lambda s: None, client=client, clock=clock)
    res = vt.run_load("http://test", cfg, make_client=lambda: BorrowedAll(client), clock=clock, sleep=clock.sleep, start_utc=start)
    assert vt.count_outcomes(res.records)["accepted"] == 2 * (3 + 30)
    v = vt.verify(Borrowed2(client), "", res.records, cfg, res.start_utc)
    assert v["overall"]["failed"] == 0 and v["overall"]["unknown"] == 0, v
    a = v["per_athlete"][0]
    assert a["retained"]["physiological"] == {"retained": 3, "span_s": 2.0, "cap": 300, "capped": False} and a["retained"]["tracking"] == {"retained": 30, "span_s": 2.9, "cap": 300, "capped": False}
    report = vt.build_report(cfg, {"base_url": "x", "pid": 0, "db_path": "x", "command": [], "retention_cap": 300}, res.records, res.actual_s, res.start_utc, v)
    assert report["streams"]["tracking"]["requested_rate_hz"] == 10.0 and report["retained_spans"]["tracking"]["rows"] == [30, 30]
    lines = []
    vt.print_summary(report, lines.append)
    assert any("tracking       requested 10 Hz" in l for l in lines) and any("retained per athlete: 30-30 rows spanning 2.9-2.9 s" in l for l in lines)


# ---- per-stream retention caps for the isolated backend --------------------------------------------------------------

def test_isolated_backend_caps_go_only_into_its_own_environment():
    b = vt.IsolatedBackend(port=1, retention_caps={"tracking": 3000})
    assert b.environment() == {"TRACKING_RETENTION_SAMPLES": "3000"}
    assert vt.IsolatedBackend(port=1).environment() == {}, "no caps: the backend's own defaults"
    for bad in ({"tracking": 0}, {"tracking": 2.5}, {"tracking": True}, {"devices": 5}):
        with pytest.raises(ValueError):
            vt.IsolatedBackend(port=1, retention_caps=bad)
    assert not (ROOT / ".env").exists() or "RETENTION" not in (ROOT / ".env").read_text(), "the tool never writes .env"


def test_expected_retained_and_cli_use_each_streams_cap():
    recs = [vt.RequestRecord("a", "tracking", t, float(t), float(t), 0.001, "accepted", 200, payload={"timestamp": (datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=t)).isoformat()}) for t in range(10)]
    assert [r.tick for r in vt.expected_retained(recs, "a", "tracking", 4)] == [6, 7, 8, 9]
    assert len(vt.expected_retained(recs, "a", "tracking")) == 10, "default cap 300 keeps all ten"
    args = vt.build_parser().parse_args(["--tracking-cap", "3000"])
    cfg = vt.RunConfig(physiological_cap=args.physiological_cap, tracking_cap=args.tracking_cap)
    assert cfg.retention_caps == {"physiological": 300, "tracking": 3000} and cfg.cap_for("tracking") == 3000
    assert vt.main(["--tracking-cap", "0"], out=lambda s: None) == 2, "rejected before any backend is started"


# ---- an active recording during the run: session reads, sizes, reviews ------------------------------------------------

def test_session_reads_of_an_active_recording_name_only_their_athlete(client):
    clock = FakeClock()
    cfg = vt.RunConfig(athletes=2, duration_s=4, physiological_rate_hz=1.0, tracking_rate_hz=2.0, workers=1, read_interval_s=0)
    vt.register_team("", cfg, out=lambda s: None, client=client, clock=clock)
    rec = client.post("/api/v1/recordings", json={"name": "fictional", "athlete_ids": [vt.athlete_id(0), vt.athlete_id(1)]}).json()["id"]
    cfg.recording_id, cfg.session_read_interval_s = rec, 1.0
    start = datetime.now(UTC) - timedelta(seconds=30)
    res = vt.run_load("http://test", cfg, make_client=lambda: BorrowedAll(client), clock=clock, sleep=clock.sleep, start_utc=start)
    assert len(res.session_reads) == 4 and all(r.outcome == "ok" for r in res.session_reads), res.session_reads
    assert [r.detail for r in res.session_reads] == [vt.athlete_id(1), vt.athlete_id(0), vt.athlete_id(1), vt.athlete_id(0)], "athletes rotate"
    assert all(v is True for r in res.session_reads for v in r.checks.values())
    snap = vt.recorded_snapshot(Borrowed2(client), "", rec, cfg)
    assert snap[vt.athlete_id(0)]["tracking"]["recorded"] == 8 and snap[vt.athlete_id(0)]["physiological"]["recorded"] == 4


def test_database_bytes_counts_the_wal_sidecars(tmp_path):
    db = tmp_path / "x.db"
    db.write_bytes(b"a" * 10)
    (tmp_path / "x.db-wal").write_bytes(b"b" * 5)
    assert vt.database_bytes(db) == 15 and vt.database_bytes(tmp_path / "missing.db") == 0


def test_record_flags_parse_with_documented_defaults():
    args = vt.build_parser().parse_args(["--record"])
    assert args.record is True and args.session_read_interval == 15.0 and args.size_interval == 60.0
    assert vt.build_parser().parse_args([]).record is False, "default behaviour is unchanged"
