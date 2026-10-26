"""scripts/verify_period_workflow.py: deterministic fictional session, period plan and argument rules (the real workflow runs against isolated backends)."""

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


vp = load("verify_period_workflow")


def test_session_payloads_are_deterministic_with_nulls_low_confidence_and_a_gap():
    ts = datetime(2026, 10, 26, 20, 0, 0, tzinfo=UTC)
    series = vp.effort_rest_heart_rate_series(120)
    a = vp.session_payloads(0, 5, series, ts)
    assert a == vp.session_payloads(0, 5, series, ts) and a["tracking"]["speed_mps"] == 3.0 and a["tracking"]["tracking_confidence"] == 1.0
    assert vp.session_payloads(0, 9, series, ts)["physiological"]["heart_rate_bpm"] is None, "every ninth second: a missing measurement"
    assert vp.session_payloads(0, 13, series, ts)["tracking"]["tracking_confidence"] == 0.2, "every thirteenth second: a low-confidence point"
    assert vp.session_payloads(0, 52, series, ts) is None and vp.session_payloads(0, 56, series, ts) is not None, "the gap sends nothing"
    assert vp.session_payloads(1, 5, series, ts)["physiological"]["heart_rate_bpm"] != a["physiological"]["heart_rate_bpm"], "athletes differ"
    assert vp.session_payloads(0, 35, series, ts)["tracking"]["speed_mps"] == 0.0, "rest is a real zero"
    plan = vp.period_plan(ts)
    assert sum(1 for p in plan if p["label"] == "second cycle effort") == 2 and all(1 <= p["end"] - p["start"] <= 120 for p in plan)
    assert any(p["label"] == "gap" and vp.GAP[0] <= p["start"] and p["end"] < vp.GAP[1] for p in plan)


def test_arguments_and_nothing_starts_for_a_short_session():
    def never(**kw):
        raise AssertionError("no backend may be started")
    lines = []
    assert vp.main(["--seconds", "50"], out=lines.append, backend_factory=never) == 2 and any("at least 100" in l for l in lines)
    assert vp.build_parser().parse_args([]).seconds == 120
