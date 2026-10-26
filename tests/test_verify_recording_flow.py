"""scripts/verify_recording_flow.py: deterministic fictional samples and argument rules (the real flow runs against isolated backends)."""

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


vf = load("verify_recording_flow")


def test_payloads_are_deterministic_distinct_per_athlete_and_explicitly_simulated():
    ts = datetime(2026, 10, 26, 20, 0, 0, tzinfo=UTC)
    a, b = vf.payloads(0, 3, ts), vf.payloads(1, 3, ts)
    assert a == vf.payloads(0, 3, ts) and a["physiological"]["heart_rate_bpm"] != b["physiological"]["heart_rate_bpm"]
    assert a["tracking"]["source"] == "simulated" and a["tracking"]["tracking_confidence"] == 1.0 and a["physiological"]["timestamp"] == "2026-10-26T20:00:00Z"
    assert a["physiological"]["athlete_id"] == "fictional-athlete-01" and b["tracking"]["device_id"] == "fictional-tracker-02"


def test_arguments_must_exceed_the_cap_and_nothing_starts_otherwise():
    def never(**kw):
        raise AssertionError("no backend may be started")
    lines = []
    assert vf.main(["--cap", "3", "--samples", "3"], out=lines.append, backend_factory=never) == 2
    assert vf.main(["--cap", "0"], out=lines.append, backend_factory=never) == 2
    assert vf.main(["--after", "0"], out=lines.append, backend_factory=never) == 2
    assert any("samples must exceed the cap" in l for l in lines)
    args = vf.build_parser().parse_args([])
    assert (args.cap, args.samples, args.after) == (3, 8, 4)
