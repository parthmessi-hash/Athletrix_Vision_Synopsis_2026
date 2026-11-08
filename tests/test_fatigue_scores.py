"""The pilot's three EXPERIMENTAL candidate scores on hand-built FICTIONAL samples: feature classes, hand-worked values,
elapsed-time rules, unavailable versus zero, wearer separation, test-effort exclusion in both streams, and the combined
score never falling back to one source."""

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.core import fatigue as fz
from app.models import PhysiologicalSample, TrackingSample

ROOT = Path(__file__).resolve().parents[1]
TEXT = (ROOT / "scripts" / "examples" / "pilot" / "fictional_fatigue_profile.json").read_text()
P, SHA = fz.parse_profile(TEXT)
T0 = datetime(2026, 11, 7, 10, tzinfo=UTC)
ALIGN = {"stream": "camera", "basis": "operator_estimate", "uncertainty_s": 0.05}


def at(s):
    return T0 + timedelta(seconds=s)


def ph(s, hr, dev="strap-1", aid="x1"):
    return (PhysiologicalSample(athlete_id=aid, timestamp=at(s), source="simulated", device_id=dev, heart_rate_bpm=hr), {"received_at": fz.iso(at(s))})


def tr(s, v, seg="seg-1", aid="x1"):
    return (TrackingSample(athlete_id=aid, timestamp=at(s), x_position=0.0, y_position=0.0, speed_mps=v, acceleration_mps2=0.0, tracking_confidence=0.9,
                           source="camera", segment_id=seg, value_basis="estimated"), {"received_at": fz.iso(at(s))})


WEARER = fz.Wearer(at(0), None, "strap-1", "P1", {"id": "bl-1", "rest_hr_bpm": 60.0, "n": 200, "status": "accepted"}, 200.0, "FICTIONAL: estimated 208 - 0.7 x age")


def inputs(phys, track, cutoff, **kw):
    return fz.ScoreInputs("x1", at(cutoff), "as_entered", phys, track, kw.pop("wearers", [WEARER]), kw.pop("exclusions", []), kw.pop("reference_start", at(0)),
                          kw.pop("camera_alignment", ALIGN))


def steady(start, end, hr, step=1.0):
    return [ph(start + k * step, hr) for k in range(int((end - start) / step) + 1)]


def moving(start, end, v, step=0.2, seg="seg-1"):
    return [tr(start + k * step, v, seg) for k in range(int((end - start) / step) + 1)]


def test_profile_classifies_features_by_their_true_inputs():
    bad = json.loads(TEXT)
    bad["candidates"]["vest_only"]["weights"]["hr_per_speed"] = 0.2
    with pytest.raises(fz.FatigueProfileError, match="hr_per_speed uses \\['camera', 'wearable'\\], not only \\['wearable'\\]"):
        fz.parse_profile(json.dumps(bad))
    bad = json.loads(TEXT)
    bad["features"]["output_change"]["inputs"] = ["wearable"]
    with pytest.raises(fz.FatigueProfileError, match="true inputs"):
        fz.parse_profile(json.dumps(bad))


def test_hand_worked_values_and_the_display_convention():
    # rest 60, HR max 200: mean 172 -> (172-60)/140 = 0.8 of reserve -> (0.8-0.5)/(0.9-0.5) = 75
    phys = steady(0, 400, 120) + steady(401, 600, 172)
    track = moving(0, 600, 3.0)
    r = fz.score(inputs(phys, track, 600), P, SHA)
    f = r["features"]
    assert f["hr_strain"]["value"] == 75.0 and f["hr_strain"]["measure"]["fraction_of_reserve"] == 0.8
    assert f["hr_strain"]["reference"]["hr_max_origin"].startswith("FICTIONAL")
    # threshold 0.85 x 200 = 170; window (300, 600]: samples 301..600, 299 one-second intervals covered; the intervals
    # starting at 401..599 s (172 bpm) are above: 199 s -> 199/299 = 0.666 of covered time -> 0.666/0.5 -> clipped to 100
    m = f["time_above_threshold"]["measure"]
    assert (m["seconds_above"], m["covered_s"], f["time_above_threshold"]["value"]) == (199.0, 299.0, 100.0)
    assert f["output_change"]["value"] == 0.0 and f["output_change"]["available"], "same speed as the reference: a real zero, not unavailable"
    assert r["display"].startswith("0-100") and "never 0" in r["display"]


def test_elapsed_time_does_not_bridge_long_gaps():
    phys = steady(0, 400, 120) + [ph(s, 180) for s in range(400, 600, 20)]   # readings 20 s apart: gaps over 5 s
    r = fz.score(inputs(phys, moving(0, 600, 3.0), 600), P, SHA)
    t = r["features"]["time_above_threshold"]
    assert t["available"] is False and "not bridged" in t["reason"]


def test_recovery_needs_a_usable_window():
    base = steady(0, 300, 120)
    effort = steady(301, 330, 180)
    rec = [ph(331 + k, 180 - k) for k in range(60)]   # falls 1 bpm per s for 60 s
    rest = steady(391, 600, 130)
    r = fz.score(inputs(base + effort + rec + rest, moving(0, 600, 3.0), 600), P, SHA)["features"]["recovery"]
    # effort ends at 330 s with 180 bpm (the last sample at or above 170 is at 341 s, HR 170): window 341-401 s
    assert r["available"] and r["window"]["start"] == fz.iso(at(341))
    early = fz.score(inputs(base + effort + rec[:20], moving(0, 360, 3.0), 360), P, SHA)["features"]["recovery"]
    assert early["available"] is False and "not complete" in early["reason"]
    interrupted = base + effort + rec[:30] + steady(361, 370, 185) + steady(371, 600, 130)
    r2 = fz.score(inputs(interrupted, moving(0, 600, 3.0), 600), P, SHA)["features"]["recovery"]
    assert r2["available"] and r2["window"]["start"] == fz.iso(at(370)), "the effort that ended later, with its own complete window"


def test_heart_rate_relative_to_speed_is_combined_excludes_near_zero_speed_and_needs_alignment():
    phys = steady(0, 600, 140)
    track = moving(0, 300, 3.0) + moving(300.2, 600, 2.4)
    r = fz.score(inputs(phys, track, 600), P, SHA)
    h = r["features"]["hr_per_speed"]
    assert h["available"] and h["inputs"] == ["wearable", "camera"] and h["measure"]["increase"] == pytest.approx(3.0 / 2.4 - 1, abs=1e-3)
    stop = moving(0, 300, 3.0) + moving(300.2, 600, 0.1)
    h2 = fz.score(inputs(phys, stop, 600), P, SHA)["features"]["hr_per_speed"]
    assert h2["available"] is False and h2["pairs"]["excluded_near_zero_speed"] > 100
    anchor = fz.score(inputs(phys, track, 600, camera_alignment={"stream": "camera", "basis": "replay_anchor"}), P, SHA)
    assert anchor["features"]["hr_per_speed"]["available"] is False and "replay_anchor" in anchor["features"]["hr_per_speed"]["reason"]
    assert anchor["candidates"]["combined"]["available"] is False and anchor["candidates"]["vest_only"]["available"]


def sprint(start, peak, seg="seg-1"):
    return [tr(start + k * 0.2, v, seg) for k, v in enumerate([4, 5, 5.8, peak, peak, 5.8, 4])]


def test_no_sprint_opportunity_is_not_a_drop():
    track = moving(0, 50, 3.0) + sprint(50.2, 7.5) + moving(52, 600, 3.0)
    r = fz.score(inputs(steady(0, 600, 140), track, 600), P, SHA)["features"]["sprint_quality"]
    assert r["available"] is False and "no sprint opportunity" in r["reason"] and r["value"] is None
    track2 = moving(0, 50, 3.0) + sprint(50.2, 7.5) + moving(52, 500, 3.0) + sprint(500.2, 6.75) + moving(502, 600, 3.0)
    r2 = fz.score(inputs(steady(0, 600, 140), track2, 600), P, SHA)["features"]["sprint_quality"]
    assert r2["available"] and r2["measure"]["drop"] == pytest.approx(0.1, abs=1e-3) and r2["value"] == pytest.approx(66.67, abs=0.01)


def test_combined_is_never_turned_into_a_one_source_score():
    phys = steady(0, 600, 150)
    track = moving(0, 560, 3.0)   # camera stops 40 s before the cutoff
    r = fz.score(inputs(phys, track, 600), P, SHA)["candidates"]
    assert r["vest_only"]["available"] and r["combined"]["available"] is False and r["combined"]["value"] is None
    assert any("camera stream" in x for x in r["combined"]["reasons"])
    r = fz.score(inputs(steady(0, 560, 150), moving(0, 600, 3.0), 600), P, SHA)["candidates"]
    assert r["camera_only"]["available"] and not r["combined"]["available"] and not r["vest_only"]["available"]


def test_a_new_wearer_never_inherits_readings_or_another_device():
    w = fz.Wearer(at(300), None, "strap-1", "P2", {"id": "bl-2", "rest_hr_bpm": 55.0, "n": 200, "status": "accepted"}, 195.0, "FICTIONAL")
    phys = steady(0, 600, 140) + [ph(s + 0.5, 150, dev="strap-9") for s in range(300, 600)]
    r = fz.score(inputs(phys, moving(0, 600, 3.0), 600, wearers=[w]), P, SHA)
    ex = r["inputs"]["excluded"]
    assert ex["outside_own_wearer_assignment"] == 300 and ex["other_device"] == 300, "readings before the assignment and from another strap are left out"
    assert r["features"]["hr_strain"]["reference"]["baseline_id"] == "bl-2"
    gone = fz.score(inputs(phys, moving(0, 600, 3.0), 600, wearers=[fz.Wearer(at(0), at(500), "strap-1", "P1", WEARER.baseline, 200.0, "x")]), P, SHA)
    assert gone["features"]["hr_strain"]["available"] is False and "not the active wearer" in gone["features"]["hr_strain"]["reason"]


def test_test_effort_exclusion_applies_to_both_streams():
    ex = [(at(400), at(420), "timed test sprint, round 1")]
    phys = steady(0, 600, 140)
    track = moving(0, 600, 3.0)
    a = fz.score(inputs(phys, track, 600, exclusions=ex), P, SHA)
    hot_p = [ph(s, 199) if 400 <= s <= 420 else x for s, x in zip(range(0, 601), phys)]
    hot_t = [tr(x[0].timestamp.timestamp() - T0.timestamp(), 9.0) if at(400) <= x[0].timestamp <= at(420) else x for x in track]
    b = fz.score(inputs(hot_p, hot_t, 600, exclusions=ex), P, SHA)
    assert {c: a["candidates"][c]["value"] for c in a["candidates"]} == {c: b["candidates"][c]["value"] for c in b["candidates"]}
    assert a["inputs"]["wearable_sha256"] == b["inputs"]["wearable_sha256"] and a["inputs"]["camera_sha256"] == b["inputs"]["camera_sha256"]
    assert a["inputs"]["excluded"]["test_effort"] == 21 + 101


def test_samples_received_after_the_cutoff_are_not_seen_as_entered():
    phys = steady(0, 600, 140)
    late = [(s, {"received_at": fz.iso(at(700))}) for s, _ in steady(590, 600, 199)]
    r = fz.score(inputs(phys[:590] + late, moving(0, 600, 3.0), 600), P, SHA)
    assert r["inputs"]["excluded"]["received_after_cutoff"] == 11
