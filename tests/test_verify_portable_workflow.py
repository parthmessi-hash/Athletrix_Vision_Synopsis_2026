"""scripts/verify_portable_workflow.py: its fictional data is deterministic and covers what the workflow claims to exercise."""

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("verify_portable_workflow", ROOT / "scripts" / "verify_portable_workflow.py")
vw = importlib.util.module_from_spec(spec); spec.loader.exec_module(vw)
T0 = datetime(2026, 10, 26, 20, 0, 0, tzinfo=UTC)


def test_unequal_rates_gaps_nulls_zeros_and_low_confidence_are_present():
    ticks = [vw.payloads(0, t, T0) for t in range(60 * vw.TRACK_HZ)]
    phys = [b for tick in ticks for s, b in tick if s == "physiological"]
    track = [b for tick in ticks for s, b in tick if s == "tracking"]
    assert len(phys) == 60 and len(track) == 240, "1 Hz physiology and 4 Hz tracking"
    assert any(b["heart_rate_bpm"] is None for b in phys) and any(b.get("breathing_rate_bpm") == 0.0 for b in phys)
    assert any(b["speed_mps"] == 0.0 for b in track) and any(b["tracking_confidence"] == 0.2 for b in track)
    assert vw.payloads(1, vw.GAP[0] * vw.TRACK_HZ, T0) == [] and vw.payloads(0, vw.GAP[0] * vw.TRACK_HZ, T0) != [], "the gap is athlete 02's only"
    assert vw.payloads(0, 7, T0) == vw.payloads(0, 7, T0)


def test_hand_series_matches_the_documented_hand_calculation():
    pairs = vw.hand_payloads(T0)
    speeds = [b["speed_mps"] for s, b in pairs if s == "tracking"]
    rates = [b["heart_rate_bpm"] for s, b in pairs if s == "physiological"]
    assert speeds == [2.0, 4.0, 6.0] and rates == [100.0, None, 104.0]
    assert (speeds[0] + speeds[1]) / 2 + (speeds[1] + speeds[2]) / 2 == vw.HAND_EXPECTED["distance_m"]
    assert sum(r for r in rates if r is not None) / 2 == vw.HAND_EXPECTED["during"][2]


def test_short_runs_are_refused_before_starting_anything():
    def never(**kw):
        raise AssertionError("no backend may be started")
    lines = []
    assert vw.main(["--seconds", "30"], out=lines.append, backend_factory=never) == 2
