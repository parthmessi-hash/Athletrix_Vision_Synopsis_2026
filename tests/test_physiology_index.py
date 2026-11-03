"""Baselines and the EXPERIMENTAL physiological index: hand-worked examples, every unavailability rule, no later data,
retrospective baselines, review decisions and the API. All values and profiles are fictional."""

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.api import review as review_api
from app.core import clock
from app.core.config import Settings
from app.core.physiology_index import baseline_stats, compute_index, select_baseline
from app.db import get_database
from app.models import Athlete, PhysiologicalSample
from app.models.baseline import Baseline, BaselineCreate, IndexProfile
from app.store.recordings import recording_store

T0 = datetime(2026, 11, 1, 10, 0, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "scripts" / "examples" / "review" / "fictional_index_profile.json"


def ts(s):
    return T0 + timedelta(seconds=s)


def z(s):
    return ts(s).isoformat().replace("+00:00", "Z")


def sample(s, device="strap-1", quality=0.9, **values):
    return PhysiologicalSample(athlete_id="a1", timestamp=ts(s), source="wearable", device_id=device, signal_quality=quality, **values)


def profile(**over):
    doc = {"format": "athletrix-physiological-index-profile", "schema_version": 1, "version": "test", "fictional": True, "window_seconds": 10,
           "expected_interval_seconds": 1, "min_input_coverage": 0.5, "staleness_seconds": 5, "min_weight_coverage": 0.5,
           "baseline": {"require_status": "accepted", "conditions": None, "min_samples": 4, "same_device_required": True, "allow_retrospective": False},
           "signals": [{"signal": "heart_rate_bpm", "transform": "z_score", "direction": "higher_is_more_strain", "zero_at": 0, "hundred_at": 4, "weight": 1, "min_samples_in_window": 5}]}
    for k, v in over.items():
        doc[k] = v
    return IndexProfile.model_validate(doc)


def make_baseline(samples, signals=("heart_rate_bpm",), status="accepted", start=0, end=100, **kw):
    body = BaselineCreate(source_recording_id="r", ranges=[{"start": z(start), "end": z(end)}], condition="seated_rest", signals=list(signals), min_samples=4, **kw)
    return Baseline(**body.model_dump(), id=f"bl-{status}-{end}", athlete_id="a1", version=1, created_at=ts(end + 1), data_start=ts(start), data_end=ts(end),
                    stats=baseline_stats(body, samples), status=status)


REST = [sample(s, heart_rate_bpm=60 if s % 2 else 70, hrv_rmssd_ms=50.0) for s in range(0, 40)]        # 20 x 60 and 20 x 70


def run(prof, base, window, at_s, timing="live_time"):
    return compute_index(prof, "sha", base, timing, [(s, None) for s in window], ts(at_s))


def test_hand_worked_z_score_and_percent_change():
    base = make_baseline(REST, signals=("heart_rate_bpm", "hrv_rmssd_ms"))
    hr = base.stats["heart_rate_bpm"]
    assert hr.included == 40 and hr.mean == 65 and math.isclose(hr.sd, 5 * math.sqrt(40 / 39)), "20 x 60 and 20 x 70: mean 65, sample SD 5 x sqrt(40/39)"
    assert base.stats["hrv_rmssd_ms"].sufficient is False and "zero variance" in base.stats["hrv_rmssd_ms"].insufficient_reason
    window = [sample(s, heart_rate_bpm=80, hrv_rmssd_ms=40.0) for s in range(191, 201)]
    r = run(profile(), base, window, 200)
    z_by_hand = (80 - 65) / (5 * math.sqrt(40 / 39))                       # 2.9623
    assert r["available"] and r["value"] == round(100 * z_by_hand / 4, 2) == 74.06
    assert r["signals"][0]["contribution"] == 74.06 and r["signals"][0]["coverage"] == 1.0
    pct = profile(baseline={"require_status": "accepted", "conditions": None, "min_samples": 4, "same_device_required": True, "allow_retrospective": False},
                  signals=[{"signal": "hrv_rmssd_ms", "transform": "percent_change", "direction": "lower_is_more_strain", "zero_at": 0, "hundred_at": 80, "weight": 1, "min_samples_in_window": 5}])
    varied = make_baseline([sample(s, hrv_rmssd_ms=45.0 if s % 2 else 55.0) for s in range(40)], signals=("hrv_rmssd_ms",))
    r = run(pct, varied, window, 200)
    assert r["value"] == 25.0, "RMSSD 40 vs baseline 50: -20 %, directed +20, 20 / 80 = 25"


def test_two_signals_weights_and_weight_coverage():
    prof = profile(signals=[{"signal": "heart_rate_bpm", "transform": "z_score", "direction": "higher_is_more_strain", "zero_at": 0, "hundred_at": 4, "weight": 3, "min_samples_in_window": 5},
                            {"signal": "breathing_rate_bpm", "transform": "z_score", "direction": "higher_is_more_strain", "zero_at": 0, "hundred_at": 4, "weight": 1, "min_samples_in_window": 5}],
                   min_weight_coverage=0.8)
    base = make_baseline([sample(s, heart_rate_bpm=60 if s % 2 else 70, breathing_rate_bpm=12 if s % 2 else 16) for s in range(40)], signals=("heart_rate_bpm", "breathing_rate_bpm"))
    window = [sample(s, heart_rate_bpm=80, breathing_rate_bpm=14) for s in range(191, 201)]
    r = run(prof, base, window, 200)
    assert r["value"] == round((3 * 100 * ((80 - 65) / (5 * math.sqrt(40 / 39))) / 4 + 1 * 0) / 4, 2) == 55.54, "breathing at its mean scores 0 and still counts"
    no_br = [sample(s, heart_rate_bpm=80) for s in range(191, 201)]
    r = run(prof, base, no_br, 200)
    assert r["available"] is False and r["input_coverage"] == 0.75 and "needs 80%" in r["reasons"][0], "missing breathing is not a zero; the index is unavailable"


def test_insufficient_stale_sparse_and_device_changes_are_explicit():
    base = make_baseline(REST)
    assert run(profile(), base, [sample(s, heart_rate_bpm=80) for s in range(191, 194)], 200)["signals"][0]["reason"].startswith("3 values in the window")
    stale = run(profile(staleness_seconds=2), base, [sample(s, heart_rate_bpm=80) for s in range(191, 198)], 200)["signals"][0]
    assert "stale" in stale["reason"] and stale["newest_age_s"] == 3
    other = run(profile(), base, [sample(s, device="strap-2", heart_rate_bpm=80) for s in range(191, 201)], 200)["signals"][0]
    assert other["window_excluded"] == {"device_changed": 10} and "did not contribute to the baseline" in other["reason"]
    low_q = run(profile(), make_baseline(REST, min_signal_quality=0.8), [sample(s, quality=0.5, heart_rate_bpm=80) for s in range(191, 201)], 200)["signals"][0]
    assert low_q["window_excluded"] == {"low_quality": 10}, "the window uses the baseline's own inclusion rules"
    few = make_baseline(REST[:3])
    assert few.stats["heart_rate_bpm"].sufficient is False and "fewer than the minimum of 4" in few.stats["heart_rate_bpm"].insufficient_reason
    assert "baseline signal insufficient" in run(profile(), few, [sample(s, heart_rate_bpm=80) for s in range(191, 201)], 200)["signals"][0]["reason"]


def test_rmssd_and_sdnn_are_never_substituted():
    base = make_baseline([sample(s, hrv_sdnn_ms=40.0 + s % 3) for s in range(40)], signals=("hrv_sdnn_ms",))
    prof = profile(signals=[{"signal": "hrv_rmssd_ms", "transform": "percent_change", "direction": "lower_is_more_strain", "zero_at": 0, "hundred_at": 80, "weight": 1, "min_samples_in_window": 5}])
    r = run(prof, base, [sample(s, hrv_rmssd_ms=30.0, hrv_sdnn_ms=30.0) for s in range(191, 201)], 200)
    assert r["available"] is False and "different statistic and are not substituted" in r["signals"][0]["reason"]


def test_no_later_observation_is_used_and_late_receipts_are_excluded():
    base = make_baseline(REST)
    window = [sample(s, heart_rate_bpm=80) for s in range(191, 201)] + [sample(s, heart_rate_bpm=200) for s in range(201, 211)]
    assert run(profile(), base, window, 200)["value"] == 74.06, "samples after T change nothing"
    late = [(s, {"received_at": z(205)}) for s in window[:10]]
    r = compute_index(profile(), "sha", base, "live_time", late, ts(200))
    assert r["available"] is False and r["excluded_received_after_review_time"] == 10, "a sample that arrived after T was not available at T"


def test_baseline_selection_status_and_retrospective_use():
    prof = profile()
    pending = make_baseline(REST, status="pending_review")
    b, timing, why = select_baseline([pending], prof, ts(200))
    assert b is None and "requires accepted" in why[0]
    later = make_baseline(REST, start=300, end=400)
    b, timing, why = select_baseline([later], prof, ts(200))
    assert b is None and "does not allow a retrospective baseline" in why[-1]
    retro = profile(baseline={"require_status": "accepted", "conditions": None, "min_samples": 4, "same_device_required": True, "allow_retrospective": True})
    b, timing, _ = select_baseline([later], retro, ts(200))
    r = compute_index(retro, "sha", b, timing, [(s, None) for s in [sample(s, heart_rate_bpm=80) for s in range(191, 201)]], ts(200))
    assert timing == "retrospective" and r["timing"] == "retrospective" and any("not what could have been calculated live" in x for x in r["reasons"])
    b, timing, _ = select_baseline([later, make_baseline(REST)], retro, ts(200))
    assert timing == "live_time" and b.data_end == ts(100), "a baseline from earlier data is preferred"


# ---- API ------------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def rec(client, monkeypatch):
    monkeypatch.setattr(clock, "utc_now", lambda: ts(5000))
    a = Athlete(id="a1", name="Fictional A", jersey_number=1, sport="soccer", team_id="t")
    r = recording_store.start("fictional", [a], ts(0))
    with get_database().transaction() as c:
        for s in REST + [sample(s, heart_rate_bpm=80) for s in range(191, 201)]:
            recording_store.write_sample(c, r.id, "physiological", s)
    recording_store.stop(r.id, ts(300))
    return r.id


def settings(monkeypatch, path=PROFILE):
    monkeypatch.setattr(review_api, "get_settings", lambda: Settings(physiological_index_profile_path=str(path) if path else None))


def test_baseline_api_records_decisions_and_the_index_needs_a_profile(client, rec, monkeypatch, tmp_path):
    body = {"source_recording_id": rec, "ranges": [{"start": z(0), "end": z(39)}], "condition": "seated_rest", "signals": ["heart_rate_bpm"], "min_samples": 4, "label": "fictional rest"}
    b = client.post("/api/v1/athletes/a1/baselines", json=body).json()
    assert b["status"] == "pending_review" and b["version"] == 1 and b["stats"]["heart_rate_bpm"]["mean"] == 65 and "not a scientific validation" in b["status_note"]
    assert client.post("/api/v1/athletes/a1/baselines", json={**body, "ranges": [{"start": z(0), "end": z(400)}]}).status_code == 422, "outside the recording"
    b = client.post(f"/api/v1/athletes/a1/baselines/{b['id']}/decision", json={"decision": "accepted", "decided_by": "fictional analyst", "note": "steady rest"}).json()
    b = client.post(f"/api/v1/athletes/a1/baselines/{b['id']}/decision", json={"decision": "rejected", "decided_by": "fictional coach", "note": "sensor moved"}).json()
    assert b["status"] == "rejected" and [d["decision"] for d in b["decisions"]] == ["accepted", "rejected"], "every decision is kept"
    settings(monkeypatch, None)
    r = client.get(f"/api/v1/recordings/{rec}/athletes/a1/physiological-index", params={"at": z(200)}).json()
    assert r["available"] is False and "no physiological index profile is configured" in r["reasons"][0]
    p = tmp_path / "p.json"
    p.write_text(profile().model_dump_json())
    settings(monkeypatch, p)
    r = client.get(f"/api/v1/recordings/{rec}/athletes/a1/physiological-index", params={"at": z(200)}).json()
    assert r["available"] is False and "rejected" in r["reasons"][0]
    b2 = client.post("/api/v1/athletes/a1/baselines", json=body).json()
    assert b2["version"] == 2
    client.post(f"/api/v1/athletes/a1/baselines/{b2['id']}/decision", json={"decision": "accepted", "decided_by": "fictional analyst", "note": "redone"})
    r = client.get(f"/api/v1/recordings/{rec}/athletes/a1/physiological-index", params={"at": z(200)}).json()
    assert r["value"] == 74.06 and r["baseline"]["version"] == 2 and r["fictional_profile"] is True and "Not a fatigue" in r["label"]
    h = client.get(f"/api/v1/recordings/{rec}/athletes/a1/physiological-index/history", params={"step_s": 50}).json()
    assert [p["at"] for p in h["points"]][:2] == [z(10), z(60)] and h["points"][3]["value"] is None, "each point uses only data available then"
    saved = client.post(f"/api/v1/recordings/{rec}/athletes/a1/reviews", json={"kind": "physiological_index", "at": z(200)}).json()
    assert saved["result"]["value"] == 74.06 and json.loads(saved["inputs"]["configuration"]["physiological_index"])["version"] == "test"
    p.write_text(profile(signals=[{"signal": "heart_rate_bpm", "transform": "z_score", "direction": "higher_is_more_strain", "zero_at": 0, "hundred_at": 8, "weight": 1, "min_samples_in_window": 5}]).model_dump_json())
    assert client.get(f"/api/v1/recordings/{rec}/athletes/a1/physiological-index", params={"at": z(200)}).json()["value"] == 37.03, "the live result follows the new profile"
    rep = client.get(f"/api/v1/recordings/{rec}/reviews/{saved['id']}/reproduce").json()
    assert rep["matches"] and rep["result"]["value"] == 74.06 and rep["configuration_differs_from_current"]["physiological_index"] is True


def test_a_baseline_needs_a_stopped_recording(client, monkeypatch):
    monkeypatch.setattr(clock, "utc_now", lambda: ts(50))
    r = recording_store.start("active", [Athlete(id="a1", name="F", jersey_number=1, sport="soccer", team_id="t")], ts(0))
    body = {"source_recording_id": r.id, "ranges": [{"start": z(0), "end": z(10)}], "condition": "seated_rest", "signals": ["heart_rate_bpm"], "min_samples": 4}
    resp = client.post("/api/v1/athletes/a1/baselines", json=body)
    assert resp.status_code == 422 and "stopped recording" in resp.json()["detail"]
