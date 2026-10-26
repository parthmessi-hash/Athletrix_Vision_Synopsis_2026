"""scripts/validate_team_scale.py: scheduling, counting, verification and cleanup, with a fake clock and a mocked backend.

No real backend process or real sleeping is involved; the end-to-end run is exercised against the app through the
TestClient on an isolated database. All athletes and values are fictional.
"""

import importlib.util
import json
import subprocess
from datetime import UTC, datetime
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
    records, actual, start = vt.run_load("http://test", cfg, make_client=lambda: client, clock=clock, sleep=clock.sleep, start_utc=START)
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
    cfg = vt.RunConfig(athletes=3, duration_s=2, rate_hz=1.0, workers=1)
    records, _, _ = vt.run_load("http://test", cfg, make_client=lambda: mock_client(handler), clock=clock, sleep=clock.sleep, start_utc=START)
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
    records, _, _ = vt.run_load("http://test", cfg, make_client=lambda: mock_client(handler), clock=clock, sleep=clock.sleep, start_utc=START)
    counts = vt.count_outcomes(records)
    assert counts["expected"] == 4 and counts["unsent"] >= 1 and counts["accepted"] + counts["unsent"] == 4
    assert all(r.sent_s is None and "deadline" in r.detail or r.outcome == "accepted" for r in records)


def test_lateness_is_reported_when_the_rate_cannot_be_kept():
    clock = FakeClock()
    cfg = vt.RunConfig(athletes=1, duration_s=3, rate_hz=1.0, workers=1)

    def handler(req):
        clock.now += 0.6  # two requests per tick at 0.6 s each: 1.2 s per tick against a 1 s schedule
        return httpx.Response(200, json={})
    records, actual, _ = vt.run_load("http://test", cfg, make_client=lambda: mock_client(handler), clock=clock, sleep=clock.sleep, start_utc=START)
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
    records, actual, start_utc = vt.run_load("http://test", cfg, make_client=lambda: Borrowed(client), clock=clock, sleep=clock.sleep, start_utc=start)
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
    records, _, start_utc = vt.run_load("http://test", cfg, make_client=lambda: Borrowed(client), clock=clock, sleep=clock.sleep, start_utc=start)
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
