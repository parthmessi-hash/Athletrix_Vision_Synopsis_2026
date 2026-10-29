"""The synthetic camera check end to end (skipped when the optional OpenCV/NumPy dependencies are not installed)."""

import importlib.util
import json
from pathlib import Path

import pytest

pytest.importorskip("cv2")
pytest.importorskip("numpy")

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("camera_synthetic_check_cli", ROOT / "scripts" / "camera_synthetic_check.py")
check = importlib.util.module_from_spec(spec); spec.loader.exec_module(check)


def test_synthetic_video_check_passes_its_structural_checks_and_reports_errors(tmp_path):
    lines = []
    assert check.main(["--workdir", str(tmp_path / "work"), "--report", str(tmp_path / "report.json")], out=lines.append) == 0, "\n".join(lines)
    r = json.loads((tmp_path / "report.json").read_text())
    assert r["passed"] and r["identity_swaps"] == 0 and r["bridged_windows"] == 0
    assert set(r["errors"]) == {"fictional-athlete-01", "fictional-athlete-02"} and r["errors"]["fictional-athlete-01"]["position"]["n"] > 200
    assert {x["colour"] for x in r["unassigned_tracks"].values()} == {"yellow", "blue"}, "the yellow target and blue's first 1.5 s are never sent"
    assert {x["reason"] for x in r["resets"]} == {"gap"} and len(r["resets"]) == 2, "the 0.6 s occlusion and the 0.6 s dropout reset; the 0.2 s dropout does not"
    assert r["calibration"]["check_points_m"]["rms"] > 0 and r["skipped"]["warm_up"] > 0
    samples = [json.loads(x)["sample"] for x in (tmp_path / "work" / "samples.jsonl").read_text().splitlines()]
    assert all(s["source"] == "simulated" for s in samples), "synthetic markers are never labelled as camera measurements"
