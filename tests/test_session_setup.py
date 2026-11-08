"""Session plan, preflight (three separate readiness areas), start / resume / status / stop / inspect through the CLI
against the in-process engine, and the local camera checks: calibration compatibility and time alignment with known
synthetic offsets, drift, a mismatched event and a replay anchor. FICTIONAL plan and synthetic geometry only."""

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.api import review as review_api
from app.core import alignment
from app.core.config import Settings
from camera_adapter import calibration as cal
from camera_adapter.setup import camera_checks, compatibility, inspect_calibration

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import camera_setup  # noqa: E402
import sideline_session as ss  # noqa: E402
import time_align  # noqa: E402

PLAN = ROOT / "scripts" / "examples" / "session" / "fictional_session_plan.json"
E = "http://testserver"


@pytest.fixture
def no_profiles(monkeypatch):
    monkeypatch.setattr(review_api, "get_settings", lambda: Settings())


def run(client, *argv):
    lines = []
    code = ss.main(list(argv), out=lines.append, client=client)
    return code, "\n".join(lines)


def test_preflight_separates_raw_recording_from_the_experimental_recommendation(client, no_profiles):
    code, text = run(client, "preflight", str(PLAN), "--engine", E)
    assert code == 0
    pf = client.post("/api/v1/preflight", json=ss.load_plan(PLAN).model_dump(mode="json")).json()
    r = pf["readiness"]
    assert r["raw_recording"]["verdict"] == "ready_with_warnings", "raw recording is not blocked by missing profiles"
    assert r["experimental_recommendation"]["verdict"] == "blocked" and any("no performance rubric" in c["text"] for c in r["experimental_recommendation"]["checks"])
    assert any("f01: no wearable reading" in c["text"] for c in r["raw_recording"]["checks"]), "an unconnected wearable is reported missing, not faked"
    assert all(not a["wearable"]["receiving"] for a in pf["athletes"])
    assert "Raw recording: READY (with warnings)" in text and "Experimental recommendation: BLOCKED" in text and "FICTIONAL" in text


def test_start_resume_status_stop_inspect(client, no_profiles):
    code, text = run(client, "start", str(PLAN), "--engine", E)
    assert code == 0 and "Started recording" in text and "Roster: 7 entries added" in text, text
    rid = client.get("/api/v1/recordings/active").json()["recording"]["id"]
    code, text = run(client, "start", str(PLAN), "--engine", E)
    assert code == 0 and f"Resuming: this plan's recording {rid}" in text and "Roster: 0 entries added, 7 already present" in text, "a retry repeats nothing"
    ros = client.get(f"/api/v1/recordings/{rid}/roster/effective", params={"at": datetime.now(UTC).isoformat()}).json()
    assert ros["athletes"]["f04"]["inputs"]["physiology"] == "not_used" and ros["athletes"]["f01"]["inputs"]["physiology_interval_s"] == 1.0
    other = json.loads(PLAN.read_text()); other["name"] = "FICTIONAL other plan"
    p2 = PLAN.parent / "_tmp_other_plan.json"
    try:
        p2.write_text(json.dumps(other))
        code, text = run(client, "start", str(p2), "--engine", E)
    finally:
        p2.unlink()
    assert code == 2 and "another recording is active" in text and "Nothing was registered or started" in text
    client.post("/api/v1/samples/physiological", json={"athlete_id": "f01", "timestamp": datetime.now(UTC).isoformat(), "source": "simulated", "heart_rate_bpm": 120.0, "device_id": "fictional-strap-f01"})
    code, text = run(client, "status", "--engine", E)
    assert code == 0 and "wearable present: f01" in text and "wearable unavailable: f02, f03, f05" in text and "wearable not_used: f04, f06" in text, text
    assert run(client, "stop", "--engine", E)[0] == 0
    code, text = run(client, "stop", "--engine", E, "--recording", rid)
    assert code == 0 and "already stopped" in text
    code, text = run(client, "inspect", rid, "--engine", E)
    assert code == 0 and "plan: FICTIONAL rehearsal session (sha256" in text and "FICTIONAL)" in text and "physiological      1 (receipts 1)" in text, text


def test_an_unreachable_engine_changes_nothing():
    code, text = run(None, "start", str(PLAN), "--engine", "http://127.0.0.1:9")
    assert code == 3 and "not reachable" in text and "Nothing was changed" in text


# ---- calibration compatibility ------------------------------------------------------------------------------------------

@pytest.fixture
def calibration(tmp_path):
    doc = json.loads((ROOT / "scripts" / "examples" / "camera" / "calibration_points_example.json").read_text())
    c = cal.fit(doc["image"]["width"], doc["image"]["height"], doc["fit_points"], doc.get("check_points") or [], camera_id="cam-synthetic",
                camera_settings={"zoom": "wide end", "mount": "tripod A"})
    p = tmp_path / "cal.json"
    c.save(p)
    return p, doc


def test_calibration_inspection_and_invalidation(calibration, tmp_path):
    p, doc = calibration
    s = inspect_calibration(p)
    assert s["valid"] and s["camera_settings"] == {"zoom": "wide end", "mount": "tripod A"} and "NOT independent" in s["fit_residual_m"]["meaning"]
    w, h = doc["image"]["width"], doc["image"]["height"]
    same = compatibility(s, {"width": w, "height": h, "camera_id": "cam-synthetic", "camera_settings": {"zoom": "wide end", "mount": "tripod A"}})
    assert not any(c["level"] == "block" for c in same)
    bad = compatibility(s, {"width": 1920, "height": 1080, "camera_id": "cam-2", "camera_settings": {"zoom": "tele end", "mount": "tripod A"}})
    texts = [c["text"] for c in bad if c["level"] == "block"]
    assert any("resolution 1920x1080 differs" in t for t in texts) and any("is not the calibrated camera" in t for t in texts) and any("zoom: 'wide end' -> 'tele end'" in t for t in texts)
    edited = json.loads(p.read_text()); edited["image_to_field"][0][0] += 0.5
    q = tmp_path / "edited.json"; q.write_text(json.dumps(edited))
    assert inspect_calibration(q)["valid"] is False, "an edited transformation is detected"
    lines = []
    assert camera_setup.main(["compatible", str(p), "--width", "1920", "--height", "1080"], out=lines.append) == 1 and lines[-1] == "NOT COMPATIBLE"
    checks = camera_checks({"calibration_path": str(p), "time_mapping_path": None, "clip_or_camera": {"width": w, "height": h}})
    assert any("no time alignment named" in c["text"] for c in checks) and any("untested" in c["text"] for c in checks)


# ---- time alignment -----------------------------------------------------------------------------------------------------

REF = datetime(2026, 11, 7, 10, 0, tzinfo=UTC)


def events(offset, drift_ppm=0.0, noise=(), times=(0, 900, 1800, 2700, 3600)):
    out = []
    for i, s in enumerate(times):
        r = REF + timedelta(seconds=s + offset + drift_ppm * 1e-6 * s + (noise[i] if i < len(noise) else 0.0))
        out.append({"label": f"e{i}", "stream_time_s": s, "reference_utc": r.isoformat().replace("+00:00", "Z")})
    return out


def test_known_offset_and_drift_are_recovered_with_residuals_and_uncertainty():
    d = alignment.fit(events(12.5), basis="operator_estimate", stream="camera", reference_uncertainty_s=0.04)
    m = alignment.map_time(d, 1800)
    assert d["model"]["kind"] == "offset_drift" and d["model"]["stream_zero_utc"] == "2026-11-07T10:00:12.500000Z" and abs(d["model"]["drift"]) < 1e-9
    assert m["stream_time_s"] == 1800 and m["mapped_utc"] == "2026-11-07T10:30:12.500000Z", "the original stream time is kept beside the mapped time"
    d = alignment.fit(events(-3.0, drift_ppm=200, noise=(0.01, -0.02, 0.015, -0.01, 0.005)), basis="capture_time_mapping", stream="camera", reference_uncertainty_s=0.02)
    assert abs(d["model"]["drift"] * 1e6 - 200) < 15 and d["uncertainty_s"] < 0.1 and not any(r["possibly_mismatched"] for r in d["residuals"])
    one = alignment.fit(events(5.0)[:1], basis="operator_estimate", stream="camera", reference_uncertainty_s=0.04)
    assert one["model"]["kind"] == "offset" and one["uncertainty_s"] == 0.04 and any("drift is unknown" in w for w in one["warnings"])
    off = alignment.fit(events(-3.0, drift_ppm=200), basis="operator_estimate", stream="camera", allow_drift=False)
    assert any("clock drift is likely" in w for w in off["warnings"]), "an offset-only fit says when the events disagree with a constant offset"


def test_a_mismatched_event_is_flagged_not_dropped_and_can_be_excluded(tmp_path):
    ev = events(7.0, noise=(0.01, -0.01, 2.0, 0.0, 0.01))
    d = alignment.fit(ev, basis="operator_estimate", stream="camera")
    assert [r["label"] for r in d["residuals"] if r["possibly_mismatched"]] == ["e2"] and len(d["residuals"]) == 5 and d["uncertainty_s"] > 1
    d2 = alignment.fit(ev, basis="operator_estimate", stream="camera", exclude=["e2"])
    assert d2["excluded"] == ["e2"] and d2["uncertainty_s"] < 0.1 and d2["model"]["stream_zero_utc"].startswith("2026-11-07T10:00:07.0")
    src = tmp_path / "events.json"; src.write_text(json.dumps({"stream": "camera", "basis": "operator_estimate", "events": ev}))
    lines = []
    assert time_align.main(["fit", str(src), "--out", str(tmp_path / "a.json")], out=lines.append) == 0 and any("POSSIBLY MISMATCHED" in x for x in lines)
    doc = json.loads((tmp_path / "a.json").read_text()); doc["model"]["offset_s"] += 1
    (tmp_path / "b.json").write_text(json.dumps(doc))
    with pytest.raises(alignment.AlignmentError, match="does not match its events"):
        alignment.load(tmp_path / "b.json")


def test_cross_stream_comparison_needs_stated_alignment_on_both_sides():
    cam = alignment.fit(events(2.0), basis="operator_estimate", stream="camera", reference_uncertainty_s=0.05)
    wear = alignment.fit(events(0.0), basis="capture_time_mapping", stream="wearable", reference_uncertainty_s=0.05)
    anchor = alignment.fit(events(0.0)[:1], basis="replay_anchor", stream="camera")
    assert alignment.cross_stream(cam, wear, 0.5)["available"] is True
    assert alignment.cross_stream(cam, wear, 0.05)["available"] is False, "combined uncertainty too large for this comparison"
    r = alignment.cross_stream(anchor, wear, 5.0)
    assert r["available"] is False and "replay anchor" in r["reasons"][0]
    assert alignment.cross_stream(None, wear, 5.0)["available"] is False, "close-looking timestamps without a stated alignment are not synchronized"
