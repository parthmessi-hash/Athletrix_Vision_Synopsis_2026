"""The pilot's paired analysis on a deterministic FICTIONAL fixture whose expected values were computed separately:
coverage and missing reasons, matched-row correlations, a participant where the combined method loses, undefined
correlations, round order, self-ratings kept apart, and the held-out guard."""

import json
from pathlib import Path

import importlib.util

from app.core.pilot_analysis import compare, pearson

ROOT = Path(__file__).resolve().parents[1]
FIX = json.loads((ROOT / "scripts" / "examples" / "pilot" / "fictional_analysis_rows.json").read_text())
EXP = FIX["expected"]
spec = importlib.util.spec_from_file_location("evaluate_pilot", ROOT / "scripts" / "evaluate.py")
ev = importlib.util.module_from_spec(spec); spec.loader.exec_module(ev)


def r(x):
    return None if x["r"] is None else round(x["r"], 3)


def test_fixture_matches_the_separately_computed_values():
    res = compare(FIX["rows"])
    assert {c: v["usable"] for c, v in res["coverage"].items()} == EXP["coverage_usable"] and res["matched_rows"] == EXP["matched_rows"]
    assert {c: round(res["pooled"][c]["r"], 4) for c in ("vest_only", "camera_only", "combined")} == EXP["pooled_r"]
    assert round(res["pooled"]["round_order"]["round_vs_slowdown"]["r"], 4) == EXP["round_vs_slowdown_r"]
    for p, want in EXP["by_participant_r"].items():
        got = {c: r(res["by_participant"][p][c]) for c in want}
        assert got == {c: None if v is None else round(v, 3) for c, v in want.items()}, p
    assert res["by_participant"]["P2"]["vest_only"]["why"].startswith("constant input")
    assert {k: v for k, v in res["targets"].items() if k != "note"} == EXP["targets_pooled"]
    assert round(res["self_rating_exploratory"]["self_rating_vs_slowdown"]["r"], 4) == EXP["self_rating_r"]
    assert "camera stream" in json.dumps(res["coverage"]["combined"]["missing_reasons"])


def test_a_losing_combined_method_is_reported_as_not_met():
    res = compare([x for x in FIX["rows"] if x["participant"] == "P1"])
    assert {k: v for k, v in res["targets"].items() if k != "note"} == EXP["targets_P1_only"]
    assert res["pooled"]["combined_minus_vest_only"] < 0 and res["pooled"]["vest_only"]["flag"].startswith("small sample")


def test_undefined_and_small_correlations_are_never_numbers():
    assert pearson([1, 2], [1, 2])["r"] is None and pearson([5, 5, 5], [1, 2, 3])["r"] is None
    assert pearson([1, 2, 3, 4], [2, 4, 6, 8]) == {"r": 1.0, "n": 4, "flag": "small sample (n < 5)"}


def test_report_and_held_out_guard(tmp_path):
    lines = []
    prof = ROOT / "scripts" / "examples" / "pilot" / "fictional_fatigue_profile.json"
    assert ev.main(["pilot-pin", "--profile", str(prof), "--analysis-split", "participant", "--held-out", "P2", "--out", str(tmp_path / "pin.json")], out=lines.append) == 0
    assert ev.main(["pilot", "--rows", str(ROOT / "scripts" / "examples" / "pilot" / "fictional_analysis_rows.json"), "--out", str(tmp_path / "r"), "--pin", str(tmp_path / "pin.json")], out=lines.append) == 0
    res = json.loads((tmp_path / "r" / "results.json").read_text())
    assert res["held_out"]["status"].startswith("NOT HELD OUT") and "3 participants" not in res["held_out"]["limits"] and "participants in total" in res["held_out"]["limits"]
    assert res["matched_rows"] == 4, "the declared held-out participant is kept out of the development figures"
    md = (tmp_path / "r" / "report.md").read_text()
    assert "FICTIONAL data" in md and "not proof of effectiveness" in md and "repeated observations" in md
