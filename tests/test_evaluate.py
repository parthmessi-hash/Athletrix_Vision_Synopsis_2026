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
    assert ev.main(["run", str(tmp_path / "ds" / "dataset.json"), "--out", str(tmp_path / "r1")], out=lines.append) == 0
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
