"""The whole FICTIONAL pilot rehearsal in accelerated mode (in-process, simulated clock): baselines, wearer changes, a
dropout, saved predictions, independent results, export, import, reproduction and the paired report."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import pilot_rehearsal  # noqa: E402


def test_accelerated_rehearsal_end_to_end(tmp_path):
    lines = []
    code = pilot_rehearsal.main(["--mode", "accelerated", "--out", str(tmp_path / "r")], out=lines.append)
    s = json.loads((tmp_path / "r" / "summary.json").read_text())
    assert code == 0, lines[-3:]
    assert s["label"].startswith("ACCELERATED") and "not a timed sideline run" in s["label"]
    assert s["reproduction_on_second_database"]["all_reproduced"] and s["reproduction_on_second_database"]["statuses"] == {"reproduced": 9}
    assert s["package"]["format_version"] == 8 and s["package"]["predictions"] == 9
    assert s["log"]["restart"]["recording_still_active"] is True, "the engine restart after P1 kept the recording"
    rem = s["removal_on_a_copy"]
    assert rem["study_entries_left_for_them"] == 0 and rem["other_study_entries_kept"] > 0 and rem["exported_package_still_holds_them"] > 0
    drop = next(r for r in s["log"]["rounds"] if r["dropout_before_sprint"])
    assert (drop["participant"], drop["round"]) == ("P2", 2)
    cam = s["log"]["camera_only_player"]
    assert cam["combined"]["value"] is None and any("not the active wearer" in x for x in cam["combined"]["reasons"]), "a camera-only player never has a combined score"
    report = json.loads((tmp_path / "r" / "report" / "results.json").read_text())
    p2r2 = next(r for r in report["rows"] if r["participant"] == "P2" and r["round"] == 2)
    assert p2r2["scores"]["combined"] is None and p2r2["scores"]["camera_only"] is not None and "stale" in p2r2["reasons"]["combined"][0]
    assert next(r for r in report["rows"] if r["participant"] == "P3" and r["round"] == 1)["outcome_missing"] == "equipment_failure"
    assert [(x["participant"], x["round"]) for x in report["ineligible"]] == [("P1", 3), ("P2", 1)], report["ineligible"]
    assert "false start" in report["ineligible"][0]["reason"] and "marker_only" in report["ineligible"][1]["reason"], "the missed save is labelled, not hidden"
    assert report["timing_evidence_counts"].get("saved_before_recorded_start / timing_gate") == 1 and report["policy"] == "confirmed_sequence"
    p3r2 = next(r for r in report["rows"] if r["participant"] == "P3" and r["round"] == 2)
    assert p3r2["scores"]["vest_only"] is not None and p3r2["scores"]["camera_only"] is None and p3r2["scores"]["combined"] is None, "a camera gap: vest stays, combined never one-source"
    assert all(t["exit"] == 0 for t in s["log"]["timing_sheet"]), s["log"]["timing_sheet"]
    p2r3 = next(r for r in report["rows"] if r["participant"] == "P2" and r["round"] == 3)
    assert p2r3["scores"]["vest_only"] is not None, "after the strap reconnects, the vest score is available again"
    assert pilot_rehearsal.main(["--mode", "accelerated", "--duration-min", "90", "--out", str(tmp_path / "x")], out=lines.append) == 2, "never a 90-minute claim"


def test_a_failure_after_export_keeps_every_earlier_artifact_and_resume_finishes(tmp_path, monkeypatch):
    lines = []
    monkeypatch.setenv("ATHLETRIX_REHEARSAL_FAIL_AFTER", "export")
    try:
        pilot_rehearsal.main(["--mode", "accelerated", "--out", str(tmp_path / "r")], out=lines.append)
        raised = False
    except RuntimeError as exc:
        raised = "deliberate failure after the export step" in str(exc)
    assert raised
    d = tmp_path / "r"
    run = json.loads((d / "run.json").read_text())
    assert run["rules_version"] >= 2 and len(run["profile"]["sha256"]) == 64 and "SYNTHETIC camera tracking" in run["workload"]["inputs"]
    ingest = json.loads((d / "ingest.json").read_text())
    assert ingest["log"]["rounds_total"] == 9 and not {k: v for k, v in ingest["failed_requests"].items() if k != "pilot_view"}
    assert json.loads((d / "export.json").read_text())["predictions"] == 9 and (d / "pilot-package.zip").is_file()
    assert not (d / "reproduce.json").exists() and not (d / "summary.json").exists(), "the run stopped after the export"
    monkeypatch.delenv("ATHLETRIX_REHEARSAL_FAIL_AFTER")
    assert pilot_rehearsal.main(["--mode", "accelerated", "--out", str(d), "--resume", str(d)], out=lines.append) == 0, lines[-3:]
    rep = json.loads((d / "reproduce.json").read_text())
    assert rep["status"] == "ok" and rep["statuses"] == {"reproduced": 9} and rep["candidate_scores_deliberately_unavailable_when_saved"]
    assert json.loads((d / "analysis.json").read_text())["status"] == "ok" and (d / "report" / "report.md").is_file()
