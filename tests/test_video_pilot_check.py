"""scripts/video_pilot_check.py: refuses without a local model (never downloads) and writes only a FICTIONAL profile.
The full run (real detector, about 80 s) is recorded in docs/fatigue-pilot-checklist.md, not repeated here."""

import importlib.util
from pathlib import Path

from app.core.fatigue import parse_profile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("video_pilot_check", ROOT / "scripts" / "video_pilot_check.py")
vpc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vpc)


def test_missing_model_is_refused_without_any_download(tmp_path, monkeypatch):
    import httpx
    monkeypatch.setattr(httpx, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no network")))
    lines = []
    assert vpc.main(["--out", str(tmp_path / "o"), "--model", str(tmp_path / "absent.onnx")], out=lines.append) == 1
    assert lines[0].startswith("MISSING MODEL:") and "never downloads" in lines[0]
    assert not any((tmp_path / "o").iterdir())


def test_the_check_profile_is_fictional_and_valid():
    import json
    p, sha = parse_profile(json.dumps(vpc.profile_doc()))
    assert p["fictional"] is True and p["version"].startswith("fictional-video-check") and p["reference"]["camera_reference_s"] == 6 and len(sha) == 64
