"""When evidence happened versus when it was entered (app/core/knowledge.py), reproduction that fails clearly, and the
remaining substitution edge cases (expired context, unknown or unavailable roster state). All data is fictional."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.api import performance as perf_api
from app.api import review as review_api
from app.core import clock
from app.core import substitution as sub
from app.core.config import Settings
from app.db import get_database
from app.models import Athlete, PhysiologicalSample
from app.models.context import ContextEntry
from app.models.roster import RosterEntry
from app.models.substitution import SubstitutionProfile
from app.store.recordings import recording_store

ROOT = Path(__file__).resolve().parents[1]
RUBRIC = ROOT / "scripts" / "examples" / "performance" / "fictional_rubric_example.json"
T0 = datetime(2026, 11, 1, 10, 0, 0, tzinfo=UTC)
REC = "/api/v1/recordings"


def ts(s):
    return T0 + timedelta(seconds=s)


def z(s):
    return ts(s).isoformat().replace("+00:00", "Z")


@pytest.fixture
def rec(client, monkeypatch):
    state = {"now": ts(0)}
    monkeypatch.setattr(clock, "utc_now", lambda: state["now"])
    athletes = [Athlete(id=a, name=f"Fictional {a}", jersey_number=n + 1, sport="soccer", team_id="t") for n, a in enumerate(("a1", "b1"))]
    r = recording_store.start("fictional", athletes, ts(0))
    with get_database().transaction() as c:
        for s in range(0, 40):
            recording_store.write_sample(c, r.id, "physiological", PhysiologicalSample(athlete_id="a1", timestamp=ts(s), source="wearable", device_id="strap",
                                                                                        heart_rate_bpm=60 if s % 2 else 70, signal_quality=0.9))
    recording_store.stop(r.id, ts(3000))
    return r.id, state


def post_at(client, state, s, url, body):
    state["now"] = ts(s)
    r = client.post(url, json=body)
    assert r.status_code == 201, r.text
    return r.json()


def pass_obs(a=0, b=300, **kw):
    return {"athlete_id": "a1", "kind": "pass", "area": "execution", "period_start": z(a), "period_end": z(b), "assessment": "assessed", "attempts": 10, **kw}


def test_later_entries_and_corrections_are_retrospective_and_as_entered_ignores_them(client, rec, monkeypatch):
    rid, state = rec
    monkeypatch.setattr(perf_api, "get_settings", lambda: Settings(performance_rubric_path=str(RUBRIC)))
    early = post_at(client, state, 400, f"{REC}/{rid}/performance", pass_obs(completions=4))
    post_at(client, state, 1000, f"{REC}/{rid}/performance", pass_obs(completions=9, supersedes=early["id"], correction_reason="recounted later"))
    post_at(client, state, 900, f"{REC}/{rid}/performance", pass_obs(a=300, b=600, completions=5))
    url = f"{REC}/{rid}/athletes/a1/performance-review"
    retro = client.get(url, params={"at": z(600)}).json()
    assert retro["areas"]["execution"]["evidence"]["passes"] == {"attempts": 20, "completions": 14, "observations": 2}
    assert retro["review_time"]["entered_after_review_time"] == 2 and retro["review_time"]["label"].startswith("retrospective: 2 counted observations were entered after")
    live = client.get(url, params={"at": z(600), "knowledge": "as_entered"}).json()
    assert live["areas"]["execution"]["evidence"]["passes"] == {"attempts": 10, "completions": 4, "observations": 1}, "only the original, as entered by then"
    assert live["review_time"]["knowledge"] == "as_entered" and [o["status"] for o in live["observations"]] == ["counted"]
    assert client.get(url, params={"knowledge": "as_entered"}).status_code == 422, "as_entered needs a review time"


def test_baselines_are_known_only_from_their_creation_and_with_the_status_decided_by_then(client, rec, monkeypatch, tmp_path):
    rid, state = rec
    prof = {"format": "athletrix-physiological-index-profile", "schema_version": 1, "version": "t", "fictional": True, "window_seconds": 10, "expected_interval_seconds": 1,
            "min_input_coverage": 0.5, "staleness_seconds": 5, "min_weight_coverage": 0.5,
            "baseline": {"require_status": "accepted", "conditions": None, "min_samples": 4, "same_device_required": True, "allow_retrospective": True},
            "signals": [{"signal": "heart_rate_bpm", "transform": "z_score", "direction": "higher_is_more_strain", "zero_at": 0, "hundred_at": 4, "weight": 1, "min_samples_in_window": 5}]}
    path = tmp_path / "p.json"; path.write_text(json.dumps(prof))
    monkeypatch.setattr(review_api, "get_settings", lambda: Settings(physiological_index_profile_path=str(path)))
    b = post_at(client, state, 3100, "/api/v1/athletes/a1/baselines", {"source_recording_id": rid, "ranges": [{"start": z(0), "end": z(19)}], "condition": "seated_rest",
                                                                       "signals": ["heart_rate_bpm"], "min_samples": 4})
    state["now"] = ts(3200)
    client.post(f"/api/v1/athletes/a1/baselines/{b['id']}/decision", json={"decision": "accepted", "decided_by": "fictional analyst", "note": "ok"})
    url = f"{REC}/{rid}/athletes/a1/physiological-index"
    assert client.get(url, params={"at": z(30)}).json()["available"] is True, "retrospective: a baseline created and accepted later is used"
    live = client.get(url, params={"at": z(30), "knowledge": "as_entered"}).json()
    assert live["available"] is False and live["knowledge"] == "as_entered" and "no physiological baseline" in live["reasons"][0], "it did not exist yet"


def test_reproduction_fails_clearly_when_recorded_samples_change(client, rec, monkeypatch, tmp_path):
    rid, state = rec
    monkeypatch.setattr(review_api, "get_settings", lambda: Settings(physiological_index_profile_path=str(ROOT / "scripts" / "examples" / "review" / "fictional_index_profile.json")))
    saved = post_at(client, state, 3100, f"{REC}/{rid}/athletes/a1/reviews", {"kind": "physiological_index", "at": z(30), "knowledge": "as_entered"})
    rep = client.get(f"{REC}/{rid}/reviews/{saved['id']}/reproduce").json()
    assert rep["status"] == "reproduced" and rep["matches"] and rep["knowledge"] == "as_entered"
    with get_database().transaction() as c:
        c.execute("UPDATE recording_physiological_samples SET payload = replace(payload, '\"heart_rate_bpm\":70.0', '\"heart_rate_bpm\":71.0') WHERE recording_id = ?", (rid,))
    rep = client.get(f"{REC}/{rid}/reviews/{saved['id']}/reproduce").json()
    assert rep["status"] == "evidence_changed" and rep["matches"] is False and rep["recorded_samples_changed_for"] == ["a1"] and "differ from the saved digest" in rep["explanation"]


# ---- the substitution core with injected series ------------------------------------------------------------------------------

PROFILE = SubstitutionProfile.model_validate_json((ROOT / "scripts" / "examples" / "review" / "fictional_substitution_profile.json").read_text())


@pytest.fixture
def series(monkeypatch):
    monkeypatch.setattr(sub, "physiology_at", lambda inp, aid, t: {"available": aid == "a1", "value": 40 if aid == "a1" else None, "reasons": [] if aid == "a1" else ["no strap"]})
    monkeypatch.setattr(sub, "performance_at", lambda inp, prof, aid, t: {"available": True, "value": 30, "reasons": [], "identity_uncertain_excluded": 0, "counted_observation_ids": []})


def roster(aid, status, able=(), created=0, role=None, **kw):
    return RosterEntry(id=f"ros-{aid}-{status}-{created}", recording_id="r", created_at=ts(created), effective_from=ts(0), source="head_coach", athlete_id=aid, status=status,
                       current_role=role, able_roles=list(able), **kw)


def inputs(entries, context=(), knowledge="retrospective"):
    athletes = {a: sub.AthleteData(Athlete(id=a, name=a, jersey_number=n + 1, sport="soccer", team_id="t")) for n, a in enumerate(("a1", "b1", "b2"))}
    return sub.ReviewInputs("r", ts(0), ts(3000), "a1", ts(1200), athletes, list(context), list(entries), (PROFILE, "sha"), None, None, knowledge)


def test_roster_entered_later_is_labelled_or_ignored(series):
    entries = [roster("a1", "on_field", ["centre back"], created=2000, role="centre back"), roster("b1", "bench", ["centre back"])]
    r = sub.substitution_review(inputs(entries))
    assert r["outcome"] == "consider_replacement" and r["knowledge"]["later_entered"] == {"roster_state": 1} and r["knowledge"]["label"].startswith("Retrospective")
    r = sub.substitution_review(inputs(entries, knowledge="as_entered"))
    assert r["outcome"] == "insufficient_evidence" and "no roster entry" in r["reasons"][0], "at the time nobody had recorded where the athlete was"


def test_expired_context_no_longer_applies_and_unavailable_athletes_are_never_options(series):
    entries = [roster("a1", "on_field", ["centre back"], role="centre back"), roster("b1", "unavailable", ["forward", "centre back"]), roster("b2", "bench", ["forward"])]
    expired = ContextEntry(id="c", recording_id="r", created_at=ts(0), effective_from=ts(0), effective_to=ts(600), source="head_coach", certainty="confirmed",
                           athlete_id="a1", role={"status": "recorded", "value": "forward"})
    r = sub.substitution_review(inputs(entries, [expired]))
    assert r["role"]["required"] == "centre back" and r["role"]["required_from"] == "roster", "the context entry ended at 600 s"
    assert [c["athlete_id"] for c in r["replacements"]] == ["b2"] and r["outcome"] == "review_later", "unavailable b1 is never offered; b2 cannot play centre back"
