"""Profile inspection (API and scripts/profile_tool.py), baseline preview and compatibility. Fictional profiles only."""

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.api import review as review_api
from app.core import clock
from app.core.config import Settings
from app.db import get_database
from app.models import Athlete, PhysiologicalSample
from app.store.recordings import recording_store

ROOT = Path(__file__).resolve().parents[1]
EX = ROOT / "scripts" / "examples"
spec = importlib.util.spec_from_file_location("profile_tool_cli", ROOT / "scripts" / "profile_tool.py")
tool = importlib.util.module_from_spec(spec); spec.loader.exec_module(tool)
FILES = [EX / "review" / "fictional_index_profile.json", EX / "review" / "fictional_substitution_profile.json", EX / "performance" / "fictional_rubric_example.json"]


def test_the_cli_inspects_and_compares_with_the_engine_parsers(tmp_path):
    lines = []
    assert tool.main(["inspect", *map(str, FILES), "--json"], out=lines.append) == 0
    doc = json.loads(lines[0])
    assert {p["kind"] for p in doc["profiles"]} == {"physiological_index", "substitution", "performance_rubric"} and all(p["fictional"] and p["valid"] for p in doc["profiles"])
    assert doc["compatibility"] == [] and all(any("FICTIONAL" in w for w in p["warnings"]) for p in doc["profiles"])
    bad = json.loads(FILES[1].read_text()); bad["physiology"]["persistence_points"] = 30
    (tmp_path / "bad.json").write_text(json.dumps(bad))
    lines = []
    assert tool.main(["inspect", str(tmp_path / "bad.json")], out=lines.append) == 1 and any("can never form" in x for x in lines)
    changed = json.loads(FILES[1].read_text()); changed["cooldown_seconds"] = 120
    (tmp_path / "same_version.json").write_text(json.dumps(changed))
    lines = []
    assert tool.main(["compare", str(FILES[1]), str(tmp_path / "same_version.json")], out=lines.append) == 1 and any("version did not change" in x for x in lines)
    changed["version"] = "fictional-substitution-2"
    (tmp_path / "new_version.json").write_text(json.dumps(changed))
    lines = []
    assert tool.main(["compare", str(FILES[1]), str(tmp_path / "new_version.json"), "--json"], out=lines.append) == 0
    assert json.loads(lines[0])["differences"] == {"cooldown_seconds": {"old": 300.0, "new": 120.0}}
    lines = []
    assert tool.main(["inspect", str(FILES[1])], out=lines.append) == 0 and tool.main(["inspect", str(FILES[1]), str(FILES[2])], out=lines.append) == 1, \
        "a substitution profile without an index profile is incompatible"


def test_the_api_reports_configured_profiles_and_none_by_default(client, monkeypatch):
    monkeypatch.setattr(review_api, "get_settings", lambda: Settings())
    r = client.get("/api/v1/profiles").json()
    assert r["profiles"] == {"physiological_index": None, "substitution": None, "performance_rubric": None}, "no profile is enabled by default"
    monkeypatch.setattr(review_api, "get_settings", lambda: Settings(physiological_index_profile_path=str(FILES[2]), substitution_profile_path=str(FILES[1])))
    r = client.get("/api/v1/profiles").json()
    assert r["profiles"]["physiological_index"]["valid"] is False and "configured as physiological_index but the file is a performance_rubric" in r["profiles"]["physiological_index"]["errors"]
    assert any("without a performance rubric" in p for p in r["compatibility"])


def test_baseline_preview_stores_nothing_and_compatibility_explains_each_baseline(client, monkeypatch):
    t0 = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
    monkeypatch.setattr(clock, "utc_now", lambda: t0 + timedelta(hours=1))
    monkeypatch.setattr(review_api, "get_settings", lambda: Settings(physiological_index_profile_path=str(FILES[0])))
    rec = recording_store.start("fictional rest", [Athlete(id="a1", name="F", jersey_number=1, sport="soccer", team_id="t")], t0)
    with get_database().transaction() as c:
        for s in range(200):
            recording_store.write_sample(c, rec.id, "physiological", PhysiologicalSample(athlete_id="a1", timestamp=t0 + timedelta(seconds=s), source="wearable", device_id="d",
                                                                                          heart_rate_bpm=60 + s % 3, breathing_rate_bpm=14.0, signal_quality=0.9))
    recording_store.stop(rec.id, t0 + timedelta(seconds=300))
    body = {"source_recording_id": rec.id, "ranges": [{"start": t0.isoformat(), "end": (t0 + timedelta(seconds=199)).isoformat()}], "condition": "seated_rest",
            "signals": ["heart_rate_bpm", "breathing_rate_bpm"], "min_samples": 30}
    pv = client.post("/api/v1/athletes/a1/baselines/preview", json=body).json()
    assert pv["stored"] is False and pv["stats"]["heart_rate_bpm"]["included"] == 200 and "zero variance" in pv["stats"]["breathing_rate_bpm"]["insufficient_reason"]
    assert client.get("/api/v1/athletes/a1/baselines").json() == []
    b = client.post("/api/v1/athletes/a1/baselines", json=body).json()
    comp = client.get("/api/v1/athletes/a1/baselines/compatibility", params={"at": (t0 + timedelta(seconds=250)).isoformat()}).json()
    row = comp["baselines"][0]
    assert row["id"] == b["id"] and row["usable"] is False and any("requires accepted" in x for x in row["reasons"]) and any("has no hrv_rmssd_ms" in x for x in row["reasons"])
    assert any("breathing_rate_bpm: zero variance" in x for x in row["reasons"]) and comp["profile"]["fictional"] is True
