"""The evaluation workbench (scripts/evaluate.py) on its FICTIONAL dataset: separate questions, preserved reviewer
disagreements, coverage next to error, the score-only variant on identical cases, and the held-out guard."""

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("evaluate_cli", ROOT / "scripts" / "evaluate.py")
ev = importlib.util.module_from_spec(spec); spec.loader.exec_module(ev)


def test_the_workbench_reports_each_question_separately_and_guards_the_held_out_split(tmp_path):
    lines = []
    assert ev.main(["make-fictional", "--out", str(tmp_path / "ds")], out=lines.append) == 0
    assert ev.main(["run", str(tmp_path / "ds" / "dataset.json"), "--out", str(tmp_path / "r1"), "--no-sensitivity"], out=lines.append) == 0
    r = json.loads((tmp_path / "r1" / "results.json").read_text())
    assert r["fictional"] is True and [c["split"] for c in r["cases"]] == ["development"], "only the requested split"
    cam = r["camera"]["by_reference"][0]
    assert cam["unmatched_coverage_gaps"] > 0 and cam["coverage"] < 1 and "repeated observations" in cam["note"], "coverage is reported next to error"
    perf = r["performance"]
    assert perf["by_reviewer"][0]["exact_agreement"] == perf["by_reviewer"][0]["compared"] and perf["disagreements_between_reviewers"], "disagreements are kept"
    sub = r["substitution"]["by_reviewer"][0]
    assert set(sub["agreement"]) == {"context_aware", "score_only"} and sub["judgments"] == 10 and "not physiological" in r["substitution"]["note"]
    assert "FICTIONAL dataset" in (tmp_path / "r1" / "report.md").read_text()
    ds = json.loads((tmp_path / "ds" / "dataset.json").read_text())
    ds["tuning_log"] = [{"date": "2026-11-03", "what": "chose a threshold after looking at results", "split_used": "held_out"}]
    (tmp_path / "ds" / "dataset.json").write_text(json.dumps(ds))
    assert ev.main(["run", str(tmp_path / "ds" / "dataset.json"), "--out", str(tmp_path / "r2"), "--split", "held_out"], out=lines.append) == 0
    r2 = json.loads((tmp_path / "r2" / "results.json").read_text())
    assert r2["held_out_status"].startswith("COMPROMISED") and [c["case_id"] for c in r2["cases"]] == ["match-held"]
    assert ev.main(["run", str(tmp_path / "ds" / "dataset.json"), "--out", str(tmp_path / "r2")], out=lines.append) == 2, "never overwrites"


def test_pilot_rehearsal_questions_variants_self_check_sensitivity_and_manifest(tmp_path):
    lines = []
    ds = tmp_path / "ds"
    assert ev.main(["make-fictional", "--out", str(ds)], out=lines.append) == 0
    assert ev.main(["manifest", str(ds / "dataset.json"), "--out", str(tmp_path / "m.json")], out=lines.append) == 0
    m = json.loads((tmp_path / "m.json").read_text())
    assert {c["case_id"]: c["split"] for c in m["cases"]} == {"match-dev": "development", "match-held": "held_out"} and m["comparison_rules"]["recommendation_variants"][0] == "descriptive"
    assert all(c["recording_id"] and c["source_types"] for c in m["cases"]) and set(m["profiles"]) == {"PHYSIOLOGICAL_INDEX_PROFILE_PATH", "SUBSTITUTION_PROFILE_PATH", "PERFORMANCE_RUBRIC_PATH"}
    assert ev.main(["run", str(ds / "dataset.json"), "--out", str(tmp_path / "r"), "--expected", str(ds / "expected-outcomes.json"), "--manifest", str(tmp_path / "m.json")], out=lines.append) == 0
    r = json.loads((tmp_path / "r" / "results.json").read_text())
    assert r["software"]["passed"] and all(c["altered_sample_detected"] and c["removed_sample_detected"] for c in r["software"]["cases"]), "Q1"
    idn = r["identity"]["by_reference"][0]
    assert idn["possible_swaps"] > 0 and {s for _, s in idn["swap_segments"]} == {"seg-p01-planted-swap"}, "Q2 flags the planted swap and nothing else"
    assert r["annotation_sheets"]["per_question"]["decision"]["both_assessed"] == 2, "Q3 blind sheets compared"
    v = r["recommendation_variants"]["by_reviewer"][0]
    assert set(v["agreement"]) == {"score_only", "context_aware", "stream_unavailable", "identity_uncertain"}
    assert v["insufficient_evidence_outputs"]["stream_unavailable"] == v["judgments"] == v["insufficient_evidence_outputs"]["identity_uncertain"], \
        "a missing stream or uncertain identity never produces a recommendation"
    assert all("physiological_index" in row["descriptive"] for row in v["rows"]), "the descriptive variant reports availability only"
    sc = r["self_check"]
    assert sc["matches"] == sc["rows"] == 10 and sc["deliberately_wrong_expectation_caught"] and sc["deliberately_broken_policy_caught"]
    assert r["sensitivity"]["split"] == "development" and len(r["sensitivity"]["steps"]) == 10 and all(st["of"] == 10 for st in r["sensitivity"]["steps"])
    other = json.loads((tmp_path / "m.json").read_text())
    other["profiles"]["SUBSTITUTION_PROFILE_PATH"]["sha256"] = "0" * 64
    (tmp_path / "m2.json").write_text(json.dumps(other))
    assert ev.main(["run", str(ds / "dataset.json"), "--out", str(tmp_path / "r3"), "--manifest", str(tmp_path / "m2.json"), "--no-sensitivity"], out=lines.append) == 1
    assert lines[-1].startswith("refused: SUBSTITUTION_PROFILE_PATH: profile differs from the manifest")
