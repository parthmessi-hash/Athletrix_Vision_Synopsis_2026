"""The EXPERIMENTAL hr_speed_matched candidate against the current hr_per_speed on FICTIONAL cases
(scripts/fatigue_method_compare.py). Behaviour only, not accuracy on athletes; the current profile and every frozen
profile are untouched."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fatigue_method_compare", ROOT / "scripts" / "fatigue_method_compare.py")
fmc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fmc)


def run(name):
    return fmc.run_case(name, fmc.CASES[name], fmc.OLD), fmc.run_case(name, fmc.CASES[name], fmc.NEW)


def test_a_slower_drill_is_not_read_as_fatigue():
    old, new = run("slower_drill_ref")
    assert old["feature"] == "hr_per_speed" and old["feature_value"] == 100, "the current ratio reads a slower drill as maximal"
    assert new["feature"] == "hr_speed_matched" and new["feature_value"] == 0
    old, new = run("slower_drill_no_ref")
    assert old["feature_value"] == 100 and new["feature_value"] is None and "does not cover this kind of movement" in new["feature_reason"]
    assert new["combined"] is None, "no confident combined estimate without a comparable reference"


def test_higher_heart_rate_at_the_same_movement_is_raised_and_robust_to_a_hidden_offset():
    old, new = run("same_work_drift")
    assert new["feature_value"] > 80 and old["feature_value"] < new["feature_value"]
    _, hidden = run("clock_offset_hidden")
    assert abs(hidden["feature_value"] - new["feature_value"]) < 10


def test_harder_work_is_not_fatigue_and_missing_inputs_give_not_enough_data():
    old, new = run("harder_work")
    assert old["feature_value"] == 0 and new["feature_value"] is None   # tradeoff: never extrapolated beyond the reference
    for case in ("clock_uncertain", "camera_missing"):
        old, new = run(case)
        assert old["combined"] is None and new["combined"] is None, case
        assert new["vest_only"] is not None or case == "clock_uncertain", "the vest-only estimate stays a separate research comparison"


def test_stop_start_stays_low_but_slightly_higher_than_before():
    old, new = run("stop_start")
    assert new["feature_value"] < 30 and old["feature_value"] < 30


def test_test_sprint_readings_never_feed_the_experimental_feature():
    """Readings inside a timed sprint's exclusion window (here wildly high heart rate and speed) leave hr_speed_matched
    exactly where it would be without them: the same exclusions as every other feature, and no bridging."""
    from datetime import timedelta

    from app.core.fatigue import ScoreInputs, Wearer, parse_profile, score
    from app.models import PhysiologicalSample, TrackingSample
    p, sha = parse_profile(fmc.NEW.read_text())
    spec = fmc.CASES["same_work_drift"]
    phys, track = fmc.simulate(spec["speed_ref"], spec["speed_recent"], spec["drift_bpm"])
    a, b = fmc.T0 + timedelta(seconds=350), fmc.T0 + timedelta(seconds=362)
    hot_p = [(PhysiologicalSample(athlete_id="fx1", timestamp=a + timedelta(seconds=k), source="simulated", device_id="vest", heart_rate_bpm=199.0), None) for k in range(1, 12)]
    hot_t = [(TrackingSample(athlete_id="fx1", timestamp=a + timedelta(seconds=k / 10), x_position=0.0, y_position=0.0, speed_mps=8.0, acceleration_mps2=0.0,
                             tracking_confidence=0.9, source="simulated", segment_id="seg-fx1", value_basis="estimated"), None) for k in range(1, 115)]
    keep = lambda rows: [x for x in rows if not (a <= x[0].timestamp <= b)]  # noqa: E731

    def run(ph, tr):
        w = Wearer(fmc.T0 - timedelta(seconds=60), None, "vest", "F1", {"id": "bl", "rest_hr_bpm": 60.0, "n": 150, "status": "accepted"}, 200.0, "FICTIONAL")
        inp = ScoreInputs("fx1", fmc.T0 + timedelta(seconds=420), "as_entered", sorted(ph, key=lambda x: x[0].timestamp), sorted(tr, key=lambda x: x[0].timestamp),
                          [w], [(a, b, "timed test sprint, round 1")], fmc.T0, {"stream": "camera", "basis": "operator_estimate", "uncertainty_s": 0.04})
        return score(inp, p, sha)["features"]["hr_speed_matched"]
    clean = run(keep(phys), keep(track))
    hot = run(keep(phys) + hot_p, keep(track) + hot_t)
    assert clean["available"] and hot["value"] == clean["value"] and hot["measure"] == clean["measure"]
