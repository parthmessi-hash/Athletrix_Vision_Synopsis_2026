"""Reference coverage for the EXPERIMENTAL movement-matched heart-rate feature (FICTIONAL data). The summary must be
built from the score's own counts and thresholds (no second decision), keep a reference that lacks a speed range apart
from missing heart rate, uncertain camera identity, a camera gap, clock alignment and an incomplete reference, and never
carry one player's reference over to another."""

import importlib.util
import json
from datetime import timedelta
from pathlib import Path

from app.api import study as study_api
from app.core.config import Settings
from app.core.fatigue import ScoreInputs, Wearer, parse_profile, score
from app.core.reference_coverage import coverage_lines, reference_coverage

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fatigue_method_compare", ROOT / "scripts" / "fatigue_method_compare.py")
fmc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fmc)
NEW = fmc.NEW


def scored(case, edit=None, wearers=None, alignment_unc=0.04, prof_path=NEW):
    p, sha = parse_profile(prof_path.read_text())
    c = fmc.CASES[case]
    phys, track = fmc.simulate(c["speed_ref"], c["speed_recent"], c.get("drift_bpm", 0.0), c.get("cam_offset_s", 0.0), c.get("camera_recent", True))
    if edit:
        phys, track = edit(phys, track)
    base = {"id": "bl-fictional", "rest_hr_bpm": fmc.REST, "n": 150, "status": "accepted"}
    ws = wearers or [Wearer(fmc.T0 - timedelta(seconds=60), None, "vest", "F1", base, fmc.HRMAX, "FICTIONAL")]
    inp = ScoreInputs("fx1", fmc.T0 + timedelta(seconds=420), "as_entered", phys, track, ws, [], fmc.T0,
                      {"stream": "camera", "basis": "operator_estimate", "uncertainty_s": alignment_unc})
    r = score(inp, p, sha)
    return r, reference_coverage(r, p), p


def secs(s):
    return (s.timestamp - fmc.T0).total_seconds()


def test_good_coverage_is_comparable_and_matches_the_score_exactly():
    r, cov, p = scored("same_work_drift")
    f = r["features"]["hr_speed_matched"]
    assert f["available"] and cov["feature_available"] and cov["status"] == "covered"
    assert cov["recent_share_in_comparable_bands"] == f["matched_share_of_recent"] >= p["features"]["hr_speed_matched"]["min_matched_share"]
    # comparable is exactly the score's matched band, nothing re-decided
    assert [b["status"] == "comparable" for b in cov["bands"]] == ["excess_bpm" in b for b in f["bands"]]
    assert cov["thresholds"]["min_pairs_per_band"] == p["features"]["hr_speed_matched"]["min_pairs_per_band"]
    assert "Nothing to fix" in cov["next_check"]
    assert any("COVERED" in x for x in coverage_lines(cov))


def test_a_recent_effort_outside_the_reference_range_is_named_as_such():
    r, cov, _ = scored("harder_work")
    assert not cov["feature_available"] and cov["status"] == "not_comparable"
    lacking = [b for b in cov["bands"] if b["status"] == "reference_lacks_range"]
    assert lacking and lacking[0]["label"] == "3.5-5 m/s" and lacking[0]["recent_pairs"] > 0
    assert "baseline-procedure decision for the team" in cov["next_check"] and "not read as fatigue" in cov["headline"]
    assert cov["lost"]["recent"]["lost"] == [], "nothing recent was lost: the reference simply never reached that speed"
    # the simulated camera starts at the round start, so the reference's first seconds have no 30 s movement context yet
    assert [x["code"] for x in cov["lost"]["reference"]["lost"]] == ["camera_gap"] and cov["lost"]["reference"]["lost"][0]["samples"] <= 20


def test_sparse_or_uneven_heart_rate_is_missing_heart_rate_not_a_reference_gap():
    # uneven: heart rate only in the first 40 s of the reference window. Enough pairs remain, so the score stands (its
    # own rule) and the summary says the reference rests on fewer samples than the window held.
    _, cov, _ = scored("same_work_drift", lambda ph, tr: ([x for x in ph if not (40 < secs(x[0]) <= 120)], tr))
    assert cov["status"] == "covered" and "heart rate is missing in the reference window" in cov["caution"]
    # sparse: one heart-rate sample every 7 s in the recent window (gaps above max_gap_s are never bridged)
    _, cov, _ = scored("same_work_drift", lambda ph, tr: ([x for x in ph if secs(x[0]) <= 300 or round(secs(x[0])) % 7 == 0], tr))
    assert cov["status"] == "covered" and "heart rate is missing in the recent window" in cov["caution"]
    # too sparse to compare: only three recent heart-rate samples
    _, cov, _ = scored("same_work_drift", lambda ph, tr: ([x for x in ph if secs(x[0]) <= 300 or round(secs(x[0])) % 40 == 0], tr))
    assert cov["status"] == "missing_heart_rate" and "recent window" in cov["reason"] and "strap contact" in cov["next_check"]
    assert not any(b["status"] == "reference_lacks_range" for b in cov["bands"])


def test_a_short_tracking_gap_is_absorbed_and_a_longer_one_is_reported():
    r0, c0, _ = scored("same_work_drift")
    r, cov, _ = scored("same_work_drift", lambda ph, tr: (ph, [x for x in tr if not (350 <= secs(x[0]) < 358)]))
    assert cov["status"] == "covered" and cov["lost"]["recent"]["lost"] == [] and cov["pairs"] == c0["pairs"], "8 s: within the 30 s context's coverage rule"
    r, cov, _ = scored("same_work_drift", lambda ph, tr: (ph, [x for x in tr if not (350 <= secs(x[0]) < 365)]))
    lost = {x["code"]: x["samples"] for x in cov["lost"]["recent"]["lost"]}
    assert cov["status"] == "covered" and lost.get("camera_gap", 0) > 0, cov["lost"]
    assert abs(r["features"]["hr_speed_matched"]["value"] - r0["features"]["hr_speed_matched"]["value"]) < 10


def test_uncertain_camera_identity_is_kept_apart_from_a_camera_gap():
    def low_conf(ph, tr):
        return ph, [(s.model_copy(update={"tracking_confidence": 0.2}), r) if secs(s) > 290 else (s, r) for s, r in tr]
    _, cov, _ = scored("same_work_drift", low_conf)
    assert cov["status"] == "camera_identity_uncertain", cov
    assert "track identity" in cov["next_check"] and "low-confidence" in cov["reason"]
    # a long gap (no samples at all) is a camera gap, not an identity problem
    _, cov, _ = scored("camera_missing")
    assert cov["status"] == "camera_gap" and "camera view" in cov["next_check"]


def test_clock_alignment_and_an_incomplete_reference_have_their_own_status():
    _, cov, _ = scored("same_work_drift", alignment_unc=2.0)
    assert cov["status"] == "clock_alignment" and cov["bands"] == []
    p, sha = parse_profile(NEW.read_text())
    c = fmc.CASES["same_work_drift"]
    phys, track = fmc.simulate(c["speed_ref"], c["speed_recent"], c["drift_bpm"])
    base = {"id": "b", "rest_hr_bpm": fmc.REST, "n": 150, "status": "accepted"}
    inp = ScoreInputs("fx1", fmc.T0 + timedelta(seconds=200), "as_entered", phys, track, [Wearer(fmc.T0 - timedelta(seconds=60), None, "vest", "F1", base, fmc.HRMAX, "F")],
                      [], fmc.T0, {"stream": "camera", "basis": "operator_estimate", "uncertainty_s": 0.04})
    cov = reference_coverage(score(inp, p, sha), p)
    assert cov["status"] == "reference_incomplete", "at 200 s the 120 s recent window still overlaps the 120 s reference"


def test_the_current_profile_has_no_coverage_summary_and_scores_are_unchanged():
    r, cov, _ = scored("same_work_drift", prof_path=fmc.OLD)
    assert cov is None and "hr_speed_matched" not in r["features"]
    assert "experimental profile only" in coverage_lines(None)[0]


def test_a_wearer_change_never_carries_one_players_reference_to_another(client, monkeypatch, tmp_path):
    """Through the engine: P1 wears the vest with a reference at 2-3 m/s and high heart rate, then P2 takes over. P2's
    coverage uses P2's own reference window (from P2's own first round start) and only P2's samples."""
    from test_study_pilot import API, PROFILE, T0, Sim, entry, hr_body, z  # noqa: PLC0415

    from app.core import clock  # noqa: PLC0415
    st = {"now": T0}
    monkeypatch.setattr(clock, "utc_now", lambda: st["now"])
    monkeypatch.setattr(clock, "receipt_now", lambda: st["now"])
    monkeypatch.setattr(study_api, "get_settings", lambda: Settings(fatigue_profile_path=str(NEW)))
    sim = Sim(client, st)
    for i, aid in enumerate(("x1", "x2")):
        client.post(f"{API}/athletes", json={"id": aid, "name": f"Fictional participant {i + 1}", "jersey_number": i + 1, "sport": "soccer", "team_id": "fictional"})
    rest = client.post(f"{API}/recordings", json={"name": "FICTIONAL rest", "athlete_ids": ["x1", "x2"]}).json()["id"]
    r0 = st["now"]
    for k in range(150):
        st["now"] = r0 + timedelta(seconds=k)
        for aid in ("x1", "x2"):
            client.post(f"{API}/samples/physiological", json=hr_body(aid, st["now"], 62 + sim.rng.gauss(0, 3), "strap-A"))
    st["now"] += timedelta(seconds=1)
    client.post(f"{API}/recordings/{rest}/stop")
    base = {}
    for aid in ("x1", "x2"):
        b = client.post(f"{API}/athletes/{aid}/baselines", json={"source_recording_id": rest, "ranges": [{"start": z(r0), "end": z(r0 + timedelta(seconds=149))}],
                                                                "condition": "seated_rest", "signals": ["heart_rate_bpm"], "min_samples": 100, "label": "FICTIONAL", "created_by": "f"}).json()
        client.post(f"{API}/athletes/{aid}/baselines/{b['id']}/decision", json={"decision": "accepted", "decided_by": "f", "note": "FICTIONAL"})
        base[aid] = b["id"]
    st["now"] = T0 + timedelta(minutes=10)
    rid = client.post(f"{API}/recordings", json={"name": "FICTIONAL coverage", "athlete_ids": ["x1", "x2"]}).json()["id"]
    sim.run(st["now"] + timedelta(seconds=3), "x1", "strap-A")
    entry(client, rid, kind="session", client_token="cov-sess-01", session_label="cov", protocol="rehearsal", fictional=True,
          clock_references=[{"stream": "camera", "basis": "operator_estimate", "uncertainty_s": 0.05}])
    entry(client, rid, kind="wearer", client_token="cov-wear-x1", athlete_id="x1", participant_code="P1", device_id="strap-A", baseline_id=base["x1"], hr_max_bpm=200, hr_max_origin="F")
    m0 = st["now"]
    entry(client, rid, kind="round_start", client_token="cov-r1-x1", athlete_id="x1", round=1)
    sim.run(m0 + timedelta(seconds=260), "x1", "strap-A", hr=lambda s: 165, v=lambda s: 2.0 + (s % 20 > 10), t0=m0)
    v1 = client.get(f"{API}/recordings/{rid}/study/pilot", params={"trend_span_s": 0}).json()
    assert v1["wearer"]["participant_code"] == "P1" and v1["reference_coverage"]["status"] == "covered", v1["reference_coverage"]
    entry(client, rid, kind="wearer", client_token="cov-wear-x2", athlete_id="x2", participant_code="P2", device_id="strap-A", baseline_id=base["x2"], hr_max_bpm=195, hr_max_origin="F")
    sim.run(st["now"] + timedelta(seconds=3), "x2", "strap-A", hr=lambda s: 120)
    rs2 = entry(client, rid, kind="round_start", client_token="cov-r1-x2", athlete_id="x2", round=1)
    v2 = client.get(f"{API}/recordings/{rid}/study/pilot", params={"trend_span_s": 0}).json()
    cov = v2["reference_coverage"]
    assert v2["wearer"]["participant_code"] == "P2" and cov["athlete_id"] == "x2"
    assert cov["status"] == "reference_incomplete" and cov["reference_window"]["start"] == rs2["at"], "P2 starts with no reference: P1's is never used"
    t2 = st["now"]
    sim.run(t2 + timedelta(seconds=250), "x2", "strap-A", hr=lambda s: 120, v=lambda s: 2.0 + (s % 20 > 10), t0=t2)
    cov = client.get(f"{API}/recordings/{rid}/study/pilot", params={"trend_span_s": 0}).json()["reference_coverage"]
    assert cov["status"] == "covered" and cov["reference_window"]["start"] == rs2["at"]
    ref = cov["lost"]["reference"]
    assert ref["heart_rate_samples"] <= 121, "only P2's own heart-rate samples in P2's own reference window"
    score_x2 = client.get(f"{API}/recordings/{rid}/study/score", params={"athlete_id": "x2"}).json()
    means = [b["reference_mean_hr"] for b in score_x2["features"]["hr_speed_matched"]["bands"] if "reference_mean_hr" in b]
    assert means and all(m < 135 for m in means), ("P2's reference heart rate is P2's (about 120), never P1's 165", means)
    assert json.dumps(cov).count("x1") == 0
    import sys as _s  # noqa: PLC0415
    _s.path.insert(0, str(ROOT / "scripts"))
    import pilot_session  # noqa: PLC0415
    lines = []
    assert pilot_session.main(["--engine", "http://testserver", "--recording", rid, "coverage"], out=lines.append, client=client) == 0
    assert "COVERED" in lines[0] and "for x2" in lines[0] and any(x.strip().startswith("next:") for x in lines), lines


def test_an_accepted_baseline_without_resting_heart_rate_is_unavailable_not_a_crash():
    """Defect fixed 2026-11-09: an accepted baseline with too few heart-rate values (no resting heart rate) raised a
    TypeError (HTTP 500) in hr_strain and hr_speed_matched. Only those two checks changed: any score that did not raise
    before is computed exactly as before."""
    for prof in (fmc.OLD, NEW):
        nb = {"id": "b", "rest_hr_bpm": None, "n": 0, "status": "accepted"}
        r, cov, _ = scored("same_work_drift", wearers=[Wearer(fmc.T0 - timedelta(seconds=60), None, "vest", "F1", nb, fmc.HRMAX, "F")], prof_path=prof)
        assert "no resting heart rate" in r["features"]["hr_strain"]["reason"] and r["candidates"]["combined"]["value"] is None
        if cov:
            assert cov["status"] == "wearer_or_baseline" and "wearer assignment" in cov["next_check"]
