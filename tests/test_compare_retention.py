"""scripts/compare_retention.py: independent movement and heart-rate expectations, effort and export checks, argument rules.

The real two-backend comparison is run by hand (see PROJECT_STATUS.md); here the checks run in-process against the app
with controlled caps and a fake clock. All data is fictional.
"""

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.core.distance import estimate_distance, estimate_high_speed
from app.models import TrackingSample

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cr = load("compare_retention")
vt = cr.sys.modules["validate_team_scale"]
START = datetime(2026, 10, 25, 11, 0, 0, tzinfo=UTC)


def rec(stream, seconds, athlete="fictional-athlete-01", outcome="accepted", **payload):
    ts = (START + timedelta(seconds=seconds)).isoformat()
    base = {"timestamp": ts, "athlete_id": athlete, "tracking_confidence": 1.0, "speed_mps": 0.0, "heart_rate_bpm": 100.0}
    return vt.RequestRecord(athlete, stream, int(seconds * 10), float(seconds), float(seconds), 0.001, outcome, 200 if outcome == "accepted" else None, payload={**base, **payload})


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.now += s


class BorrowedAll:
    def __init__(self, client):
        self.client = client

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json=None):
        return self.client.post(url.replace("http://test", ""), json=json)

    def get(self, url, params=None):
        return self.client.get(url.replace("http://test", ""), params=params)


@pytest.fixture
def caps(monkeypatch):
    def configure(physiological, tracking):
        monkeypatch.setenv("PHYSIOLOGICAL_RETENTION_SAMPLES", str(physiological))
        monkeypatch.setenv("TRACKING_RETENTION_SAMPLES", str(tracking))
        get_settings.cache_clear()
    yield configure
    get_settings.cache_clear()


# ---- independent expectations ------------------------------------------------------------------------------------

def test_independent_movement_by_hand_trapezoid_crossing_gap_and_confidence_rules():
    samples = [rec("tracking", 0, speed_mps=4.0), rec("tracking", 1, speed_mps=6.0), rec("tracking", 2, speed_mps=6.0)]
    m = cr.independent_movement(samples)
    assert m["sample_count"] == 3 and m["estimated_distance_m"] == pytest.approx(11.0) and m["included_duration_seconds"] == 2.0 and m["excluded_interval_count"] == 0
    assert m["high_speed_duration_seconds"] == pytest.approx(1.5), "rising crossing at 0.5 s plus one full interval"
    assert m["high_speed_distance_m"] == pytest.approx((5 + 6) / 2 * 0.5 + 6.0)
    gap = [rec("tracking", 0, speed_mps=6.0), rec("tracking", 4, speed_mps=6.0)]                    # 4 s > max gap: excluded, nothing bridged
    assert cr.independent_movement(gap) == {"sample_count": 2, "estimated_distance_m": None, "included_duration_seconds": 0.0, "excluded_interval_count": 1, "high_speed_duration_seconds": None, "high_speed_distance_m": None}
    low = [rec("tracking", 0, speed_mps=6.0, tracking_confidence=0.2), rec("tracking", 1, speed_mps=6.0)]
    assert cr.independent_movement(low)["estimated_distance_m"] is None, "low confidence at one end excludes the interval"
    assert cr.independent_movement([rec("tracking", 0, speed_mps=3.0)])["estimated_distance_m"] is None and cr.independent_movement([])["sample_count"] == 0
    falling = [rec("tracking", 0, speed_mps=7.0), rec("tracking", 1, speed_mps=3.0)]
    f = cr.independent_movement(falling)
    assert f["high_speed_duration_seconds"] == pytest.approx(0.5) and f["high_speed_distance_m"] == pytest.approx((7 + 5) / 2 * 0.5)
    touch = [rec("tracking", 0, speed_mps=5.0), rec("tracking", 1, speed_mps=3.0)]
    assert cr.independent_movement(touch)["high_speed_duration_seconds"] == pytest.approx(0.0), "touching the threshold at an endpoint contributes nothing"


def test_independent_movement_agrees_with_the_engine_on_simulator_samples():
    cfg = vt.RunConfig(athletes=3, duration_s=60, physiological_rate_hz=1.0, tracking_rate_hz=10.0)
    for i in range(3):
        payloads = [vt.sample_payloads(i, t, cfg.duration_s, 10.0, START + timedelta(seconds=t / 10.0))["tracking"] for t in range(50, 151)]
        recs = [vt.RequestRecord(vt.athlete_id(i), "tracking", t, t / 10.0, t / 10.0, 0.001, "accepted", 200, payload=p) for t, p in zip(range(50, 151), payloads)]
        mine = cr.independent_movement(recs)
        models = [TrackingSample.model_validate(p) for p in payloads]
        d = estimate_distance(models, 10.0); hs = estimate_high_speed(models)
        assert mine["estimated_distance_m"] == pytest.approx(d.estimated_distance_m, abs=1e-9) and mine["included_duration_seconds"] == pytest.approx(d.included_duration_seconds)
        assert mine["high_speed_duration_seconds"] == pytest.approx(hs.high_speed_duration_seconds, abs=1e-9) and mine["high_speed_distance_m"] == pytest.approx(hs.high_speed_distance_m, abs=1e-9)
        assert mine["high_speed_duration_seconds"] > 0, "the early effort crosses 5 m/s on the simulator profile"


def test_expected_periods_use_half_open_boundaries_and_the_captured_time_cap():
    phys = [rec("physiological", s, heart_rate_bpm=100 + s) for s in range(0, 60)]
    start, end = START + timedelta(seconds=15), START + timedelta(seconds=25)
    e = cr.expected_periods(phys, start, end, START + timedelta(seconds=40))
    assert (e["before"]["sample_count"], e["before"]["first"], e["before"]["last"]) == (10, (START + timedelta(seconds=5)).isoformat(), (START + timedelta(seconds=14)).isoformat())
    assert e["during"]["sample_count"] == 11 and e["during"]["mean"] == pytest.approx(120.0)
    assert e["after"]["sample_count"] == 15 and e["after"]["last"] == (START + timedelta(seconds=40)).isoformat(), "(25, 40] because the captured time caps the after period"
    assert cr.expected_periods([], start, end, end)["during"] == {"sample_count": 0, "first": None, "last": None, "mean": None}


def test_effort_outside_default_cap_rule():
    cfg = vt.RunConfig(athletes=1, duration_s=60, physiological_rate_hz=1.0, tracking_rate_hz=10.0)
    assert cr.effort_outside_default_cap(cfg, cr.Effort(5, 15)) is True, "the newest 300 of 600 ticks begin at 30 s"
    assert cr.effort_outside_default_cap(cfg, cr.Effort(25, 35)) is False
    assert cr.effort_outside_default_cap(vt.RunConfig(athletes=1, duration_s=20, physiological_rate_hz=1.0, tracking_rate_hz=10.0), cr.Effort(5, 15)) is False, "a 20 s run never crosses the cap"


# ---- checks against the app in-process -------------------------------------------------------------------------------

def run_small(client, caps_env, caps_dict):
    caps_env(**caps_dict)
    clock = FakeClock()
    cfg = vt.RunConfig(athletes=2, duration_s=3, physiological_rate_hz=1.0, tracking_rate_hz=10.0, workers=1, read_interval_s=0, physiological_cap=caps_dict["physiological"], tracking_cap=caps_dict["tracking"])
    vt.register_team("", cfg, out=lambda s: None, client=BorrowedAll(client), clock=clock)
    res = vt.run_load("http://test", cfg, make_client=lambda: BorrowedAll(client), clock=clock, sleep=clock.sleep, start_utc=START)
    return cfg, res


def test_effort_check_under_a_small_tracking_cap_explains_the_null_distance(client, caps, monkeypatch):
    from app.core import clock as backend_clock
    monkeypatch.setattr(backend_clock, "utc_now", lambda: START + timedelta(seconds=120))
    cfg, res = run_small(client, caps, {"physiological": 300, "tracking": 5})
    out = cr.check_effort(BorrowedAll(client), "", res.records, cfg, res.start_utc, cr.Effort(0, 1), {"physiological": 300, "tracking": 5})
    assert out["overall"] == {"checked": 2, "passed": 2, "failed": 0, "unknown": 0}, out
    a = out["per_athlete"][0]
    assert a["movement"]["sample_count"] == 0 and a["movement"]["estimated_distance_m"] is None and a["checks"]["missing_distance_stays_null"] is True
    assert a["movement_availability"] == {"max_samples": 5, "retained_count": 5, "retained_earliest": (START + timedelta(seconds=2.5)).isoformat().replace("+00:00", "Z"), "at_cap": True, "selected_count": 0}
    assert a["availability_explains_absence"] is True, "retained history begins after the effort and the stream is at its cap"
    assert a["heart_rate"]["during"]["sample_count"] == 2 and a["heart_rate"]["before"]["sample_count"] == 0


def test_effort_check_under_the_default_caps_matches_independent_figures(client, caps, monkeypatch):
    from app.core import clock as backend_clock
    monkeypatch.setattr(backend_clock, "utc_now", lambda: START + timedelta(seconds=120))
    cfg, res = run_small(client, caps, {"physiological": 300, "tracking": 300})
    out = cr.check_effort(BorrowedAll(client), "", res.records, cfg, res.start_utc, cr.Effort(0, 1), {"physiological": 300, "tracking": 300})
    assert out["overall"]["passed"] == 2 and out["overall"]["failed"] == 0, out
    a = out["per_athlete"][1]
    assert a["movement"]["sample_count"] == 11 and a["movement"]["estimated_distance_m"] == pytest.approx(a["expected_movement"]["estimated_distance_m"])
    assert a["movement_availability"]["at_cap"] is False and a["movement_availability"]["selected_count"] == 11 and a["availability_explains_absence"] is None
    # a wrong figure is a failure, a timed-out stream makes a mismatch unknown
    recs = list(res.records)
    recs[0] = vt.RequestRecord(recs[0].athlete, recs[0].stream, recs[0].tick, recs[0].scheduled_s, recs[0].sent_s, None, "timeout", None, payload=recs[0].payload)
    unknown = cr.check_effort(BorrowedAll(client), "", recs, cfg, res.start_utc, cr.Effort(0, 1), {"physiological": 300, "tracking": 300})
    assert unknown["per_athlete"][0]["status"] in ("unknown", "passed") and unknown["per_athlete"][1]["status"] == "passed"


def test_export_and_replay_check_in_process(client, caps, monkeypatch):
    from app.core import clock as backend_clock
    monkeypatch.setattr(backend_clock, "utc_now", lambda: START + timedelta(seconds=120))
    cfg, res = run_small(client, caps, {"physiological": 300, "tracking": 7})

    class FakeBackend:
        def __init__(self, retention_caps=None):
            self.retention_caps, self.port, self.base_url, self.db_path, self.proc = retention_caps, 1, "http://test", "x", None
            self.cleaned = False

        def start(self):
            pass

        def cleanup(self):
            self.cleaned = True
    made = []

    def factory(caps_):
        b = FakeBackend(caps_); made.append(b); return b

    def fake_replay(argv, out):      # replays the exported file into the same app: equal-instant rows are replaced, nothing changes
        for line in Path(argv[0]).read_text().splitlines():
            r = json.loads(line)
            assert client.post(f"/api/v1/samples/{r['type']}", json=r["sample"]).status_code == 200
        return 0
    out = cr.check_export_and_replay(BorrowedAll(client), "", vt.athlete_id(0), res.records, {"physiological": 300, "tracking": 7}, factory, lambda s: None, replay=fake_replay, client_factory=lambda: BorrowedAll(client))
    assert out["exported"] == {"physiological": 3, "tracking": 7} and out["headers"]["X-Athletrix-Tracking-Cap"] == "7"
    assert all(v is True for v in out["checks"].values()), out
    assert made and made[0].retention_caps == {"physiological": 300, "tracking": 7} and made[0].cleaned


# ---- argument rules -----------------------------------------------------------------------------------------------------

def test_main_rejects_an_effort_inside_the_default_cap_and_bad_values_before_starting_anything():
    def never(**kw):
        raise AssertionError("no backend may be started")
    lines = []
    assert cr.main(["--duration", "20"], out=lines.append, backend_factory=never) == 2 and any("must end before the oldest tracking sample" in l for l in lines)
    assert cr.main(["--effort-start", "10", "--effort-end", "10"], out=lines.append, backend_factory=never) == 2
    assert cr.main(["--tracking-cap", "0"], out=lines.append, backend_factory=never) == 2
    assert cr.main(["--athletes", "0"], out=lines.append, backend_factory=never) == 2
    args = cr.build_parser().parse_args([])
    assert (args.athletes, args.duration, args.physiological_rate, args.tracking_rate, args.physiological_cap, args.tracking_cap, args.effort_start, args.effort_end) == (20, 60.0, 1.0, 10.0, 300, 3000, 5, 15)
