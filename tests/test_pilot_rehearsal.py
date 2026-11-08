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
    assert s["reproduction_on_second_database"]["all_reproduced"] and len(s["reproduction_on_second_database"]["statuses"]) == 9
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
    assert [(x["participant"], x["round"]) for x in report["ineligible"]] == [("P1", 3)] and "false start" in report["ineligible"][0]["reason"]
    assert pilot_rehearsal.main(["--mode", "accelerated", "--duration-min", "90", "--out", str(tmp_path / "x")], out=lines.append) == 2, "never a 90-minute claim"
