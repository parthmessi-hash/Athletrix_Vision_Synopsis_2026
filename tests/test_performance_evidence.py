"""Individual-performance evidence: validation, sources, unavailable composites, a FICTIONAL rubric calculation,
storage, package round trip with re-pointed context references, and removal. All data and rubric values are fictional."""

import importlib.util
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api import performance as perf_api
from app.core import clock
from app.core.config import Settings
from app.core.recording_package import read_package
from app.db import Database, set_database
from app.main import create_app
from app.models.performance import PerformanceObservationCreate
from app.store.recording_import import import_package

ROOT = Path(__file__).resolve().parents[1]
RUBRIC = ROOT / "scripts" / "examples" / "performance" / "fictional_rubric_example.json"
spec = importlib.util.spec_from_file_location("local_data_cli_perf", ROOT / "scripts" / "local_data.py")
local_data = importlib.util.module_from_spec(spec); spec.loader.exec_module(local_data)

T0 = datetime(2026, 10, 30, 15, 0, 0, tzinfo=UTC)
REC = "/api/v1/recordings"


def z(s):
    return (T0 + timedelta(seconds=s)).isoformat().replace("+00:00", "Z")


def obs(athlete="a1", kind="pass", area="execution", a=0, b=600, assessment="assessed", **kw):
    return {"athlete_id": athlete, "kind": kind, "area": area, "period_start": z(a), "period_end": z(b), "assessment": assessment, "role": "left back", **kw}


@pytest.fixture
def rec(client, monkeypatch):
    state = {"now": T0}
    monkeypatch.setattr(clock, "utc_now", lambda: state["now"])
    for n, a in enumerate(("a1", "a2"), start=1):
        assert client.post("/api/v1/athletes", json={"id": a, "name": f"Fictional {a}", "jersey_number": n, "sport": "soccer", "team_id": "t"}).status_code == 201
    rid = client.post(REC, json={"name": "fictional match", "athlete_ids": ["a1", "a2"]}).json()["id"]
    state["now"] = T0 + timedelta(seconds=3000)
    client.post(f"{REC}/{rid}/stop")
    return rid


def rubric(monkeypatch, path=RUBRIC):
    monkeypatch.setattr(perf_api, "get_settings", lambda: Settings(performance_rubric_path=str(path) if path else None))


# ---- the model ---------------------------------------------------------------------------------------------------------

def test_counts_states_and_areas_are_validated():
    ok = PerformanceObservationCreate.model_validate(obs(attempts=10, completions=7))
    assert ok.evidence_source == "human_annotation" and not ok.reference_label
    for bad in (obs(attempts=5, completions=6), obs(attempts=0, completions=0), obs(kind="pass", area="decision_making", attempts=3, completions=2),
                obs(assessment="unable_to_assess"), obs(assessment="no_opportunity", attempts=1, completions=1), obs(kind="turnover", possessions=4, turnovers=5),
                obs(kind="tactical_observation", area="coach_strategy", outcome="followed"), obs(attempts=3, completions=2, possessions=3),
                obs(attempts=3, completions=2, event_times=[z(700)]), obs(attempts=3, completions=2, a=10, b=5)):
        with pytest.raises(ValidationError):
            PerformanceObservationCreate.model_validate(bad)
    unable = PerformanceObservationCreate.model_validate(obs(assessment="unable_to_assess", unable_reason="occluded"))
    assert unable.attempts is None, "unable to assess carries no counts, never a zero"


# ---- the API -------------------------------------------------------------------------------------------------------------

def test_manual_evidence_is_never_automatic_and_stays_within_the_recording(client, rec):
    r = client.post(f"{REC}/{rec}/performance", json=obs(attempts=10, completions=8, evidence_source="camera_automatic"))
    assert r.status_code == 422 and "no automatic camera event recognition" in r.json()["detail"]
    assert client.post(f"{REC}/{rec}/performance", json=obs(attempts=1, completions=1, a=-10, b=5)).status_code == 422
    assert client.post(f"{REC}/{rec}/performance", json=obs(athlete="ghost", attempts=1, completions=1)).status_code == 404
    assert client.post(f"{REC}/{rec}/performance", json=obs(kind="tactical_observation", area="coach_strategy", outcome="followed", instruction="hold the line", coach_context_id="ctx-nope")).status_code == 404
    other = client.post(f"{REC}/{rec}/context", json={"athlete_id": "a2", "effective_from": z(0), "role": {"status": "recorded", "value": "striker"}, "source": "head_coach", "certainty": "confirmed"}).json()
    assert client.post(f"{REC}/{rec}/performance", json=obs(kind="tactical_observation", area="coach_strategy", outcome="followed", instruction="press", coach_context_id=other["id"])).status_code == 422
    r = client.post(f"{REC}/{rec}/performance", json=obs(attempts=10, completions=8, evidence_source="human_review_of_camera", reference_label=True, annotator="fictional analyst",
                                                          frames=[{"video": "match.mp4", "frame": 1200, "video_time_s": 48.0}], event_times=[z(48)]))
    assert r.status_code == 201 and r.json()["id"].startswith("obs-") and r.json()["frames"][0]["frame"] == 1200


def dec(kind="decision", area="decision_making", good=1, acceptable=1, poor=0, **kw):
    return obs(kind=kind, area=area, opportunities=good + acceptable + poor, rated_good=good, rated_acceptable=acceptable, rated_poor=poor,
               criterion="chose the option that broke the press" if kind == "decision" else "kept the back-line height", **kw)


def test_without_a_rubric_the_review_shows_evidence_and_no_composite(client, rec, monkeypatch):
    rubric(monkeypatch, None)
    for body in (obs(attempts=10, completions=8, b=300), obs(attempts=6, completions=3, a=300), obs(kind="turnover", possessions=12, turnovers=2),
                 obs(assessment="unable_to_assess", unable_reason="off_camera"), dec(good=2, acceptable=1, poor=1),
                 obs(kind="positioning", area="movement_positioning", assessment="no_opportunity")):
        assert client.post(f"{REC}/{rec}/performance", json=body).status_code == 201, body
    rv = client.get(f"{REC}/{rec}/athletes/a1/performance-review").json()
    ex = rv["areas"]["execution"]["evidence"]
    assert ex["passes"] == {"attempts": 16, "completions": 11, "observations": 2} and ex["possessions"] == {"possessions": 12, "turnovers": 2, "observations": 1}
    assert ex["status"]["unable_to_assess"] == 1 and ex["unable_reasons"] == {"off_camera": 1}
    assert rv["areas"]["movement_positioning"]["evidence"]["status"] == {"no_opportunity": 1}
    assert rv["areas"]["decision_making"]["evidence"]["judged"] == {"opportunities": 4, "good": 2, "acceptable": 1, "poor": 1, "observations": 1}
    assert rv["rubric"] is None and rv["composite"] == {"available": False, "value": None, "fictional_rubric": False, "reasons": [rv["composite"]["reasons"][0]]}
    assert "no performance rubric is configured" in rv["composite"]["reasons"][0] and all(a["score"] is None for a in rv["areas"].values())


def test_each_area_needs_its_own_evidence_and_instruction_following_is_not_a_decision(client, rec):
    r = client.post(f"{REC}/{rec}/performance", json=obs(kind="tactical_observation", area="decision_making", outcome="followed", instruction="switch play"))
    assert r.status_code == 422 and "record decisions as 'decision'" in r.json()["detail"]
    for bad in (dec(good=1, acceptable=1, poor=0) | {"opportunities": 3}, dec() | {"criterion": None}, dec(kind="decision", area="movement_positioning"),
                obs(attempts=3, completions=2, criterion="x"), dec() | {"supersedes": "obs-x"}):
        assert client.post(f"{REC}/{rec}/performance", json=bad).status_code == 422, bad


def test_a_fictional_rubric_scores_only_with_enough_evidence_in_all_four_areas(client, rec, monkeypatch):
    rubric(monkeypatch)
    post = lambda body: client.post(f"{REC}/{rec}/performance", json=body)   # noqa: E731
    post(obs(attempts=10, completions=8)); post(obs(kind="turnover", possessions=10, turnovers=2))
    post(dec(good=1, acceptable=1))
    post(dec(kind="positioning", area="movement_positioning", good=1, acceptable=1))
    post(obs(kind="tactical_observation", area="coach_strategy", outcome="followed", instruction="press after a loss"))
    rv = client.get(f"{REC}/{rec}/athletes/a1/performance-review").json()
    assert rv["rubric"]["version"] == "fictional-example-2" and rv["rubric"]["fictional"] is True and len(rv["rubric"]["sha256"]) == 64
    # by hand: pass 0.8 -> (0.8-0.5)/0.4 = 75; retention 0.8 -> (0.8-0.6)/0.4 = 50; (2*75 + 1*50)/3 = 66.67
    assert rv["areas"]["execution"]["score"] == 66.67
    assert rv["areas"]["decision_making"]["score"] == 75.0 and rv["areas"]["movement_positioning"]["score"] == 75.0, "(1 + 0.5 x 1) / 2"
    assert rv["areas"]["coach_strategy"]["score"] is None and "fewer than the rubric's minimum of 2" in rv["areas"]["coach_strategy"]["unavailable_reason"]
    assert rv["composite"]["available"] is False and any(r.startswith("coach_strategy") for r in rv["composite"]["reasons"])
    post(obs(kind="tactical_observation", area="coach_strategy", outcome="not_followed", instruction="press after a loss", a=600, b=900))
    rv = client.get(f"{REC}/{rec}/athletes/a1/performance-review").json()
    assert rv["areas"]["coach_strategy"]["score"] == 50.0
    assert rv["composite"] == {"available": True, "value": round((66.67 + 75 + 75 + 50) / 4, 2), "fictional_rubric": True, "reasons": []}
    assert client.get(f"{REC}/{rec}/athletes/a2/performance-review").json()["composite"]["available"] is False, "no evidence for a2: unavailable, never zero"
    early = client.get(f"{REC}/{rec}/athletes/a1/performance-review", params={"at": z(700)}).json()
    assert early["areas"]["coach_strategy"]["score"] is None and early["review_time"]["at"] == z(700), "the observation ending at 900 s is not used at 700 s"


def test_corrections_supersede_overlaps_are_flagged_and_uncertain_identity_is_not_scored(client, rec, monkeypatch):
    rubric(monkeypatch)
    post = lambda body: client.post(f"{REC}/{rec}/performance", json=body)   # noqa: E731
    first = post(obs(attempts=10, completions=4, b=300)).json()
    fix = post(obs(attempts=10, completions=8, b=300, supersedes=first["id"], correction_reason="miscounted from the video")).json()
    assert post(obs(attempts=10, completions=9, b=300, supersedes=first["id"], correction_reason="again")).status_code == 409, "only the latest version can be corrected"
    assert client.delete(f"{REC}/{rec}/athletes/a1/performance/{first['id']}").status_code == 409, "superseded observations are audit history"
    rv = client.get(f"{REC}/{rec}/athletes/a1/performance-review").json()
    st = {o["id"]: o["status"] for o in rv["observations"]}
    assert st == {first["id"]: "superseded", fix["id"]: "counted"} and rv["areas"]["execution"]["evidence"]["passes"]["attempts"] == 10, "never both versions"
    assert [o["superseded_by"] for o in rv["observations"] if o["id"] == first["id"]] == [fix["id"]]
    dup = post(obs(attempts=5, completions=5, a=200, b=500, annotator="second analyst")).json()
    rv = client.get(f"{REC}/{rec}/athletes/a1/performance-review").json()
    st = {o["id"]: o["status"] for o in rv["observations"]}
    assert st[fix["id"]] == st[dup["id"]] == "potential_duplicate" and rv["areas"]["execution"]["evidence"]["passes"]["attempts"] == 0
    assert any("potential duplicates" in r for r in rv["composite"]["reasons"])
    distinct = post(obs(attempts=5, completions=5, a=200, b=500, distinct_from=[fix["id"]], supersedes=dup["id"], correction_reason="different passes: second half of the overlap")).json()
    rv = client.get(f"{REC}/{rec}/athletes/a1/performance-review").json()
    assert rv["areas"]["execution"]["evidence"]["passes"]["attempts"] == 15 and {fix["id"], distinct["id"]} <= set(rv["counted_observation_ids"])
    unsure = post(dec(identity="uncertain", evidence_source="human_review_of_camera")).json()
    rv = client.get(f"{REC}/{rec}/athletes/a1/performance-review").json()
    assert {o["id"]: o["status"] for o in rv["observations"]}[unsure["id"]] == "identity_uncertain" and rv["areas"]["decision_making"]["evidence"]["counted"] == 0
    assert client.delete(f"{REC}/{rec}/athletes/a1/performance/{fix['id']}").status_code == 204
    rv = client.get(f"{REC}/{rec}/athletes/a1/performance-review").json()
    assert {o["id"]: o["status"] for o in rv["observations"]}[first["id"]] == "potential_duplicate", "withdrawing the correction makes the earlier version count again; it overlaps the later one"


def test_an_invalid_or_version_1_rubric_is_an_error_not_a_silent_default(client, rec, monkeypatch, tmp_path):
    bad = tmp_path / "r.json"
    bad.write_text(json.dumps({**json.loads(RUBRIC.read_text()), "areas": {"execution": {"weight": 1, "measures": [{"measure": "instruction_followed", "weight": 1, "floor": 0, "ceiling": 1, "partial_credit": 0.5, "min_opportunities": 1}]}}}))
    rubric(monkeypatch, bad)
    r = client.get(f"{REC}/{rec}/athletes/a1/performance-review")
    assert r.status_code == 500 and "cannot score area execution" in r.json()["detail"]
    old = {k: v for k, v in json.loads(RUBRIC.read_text()).items() if k != "schema_version"}
    bad.write_text(json.dumps(old))
    r = client.get(f"{REC}/{rec}/athletes/a1/performance-review")
    assert r.status_code == 500 and "schema_version" in r.json()["detail"]


def test_legacy_version_1_tactical_observations_stay_readable_but_are_not_scored():
    from app.core.performance import review
    from app.models.performance import PerformanceObservation
    legacy = PerformanceObservation.model_validate(obs(kind="tactical_observation", area="decision_making", outcome="followed", instruction="switch play") | {"id": "obs-old", "recording_id": "r", "created_at": z(0)})
    rv = review([legacy], None, None)
    assert rv["observations"][0]["status"] == "legacy_unscored" and rv["areas"]["decision_making"]["evidence"]["counted"] == 0


def test_evidence_survives_export_import_with_context_references_and_is_removed_with_its_athlete(client, rec, isolated_db, tmp_path):
    ctx = client.post(f"{REC}/{rec}/context", json={"effective_from": z(0), "tactical_instruction": {"status": "recorded", "value": "press after a loss"}, "source": "head_coach", "certainty": "confirmed"}).json()
    a = client.post(f"{REC}/{rec}/performance", json=obs(kind="tactical_observation", area="coach_strategy", outcome="followed", instruction="press after a loss", coach_context_id=ctx["id"])).json()
    b0 = client.post(f"{REC}/{rec}/performance", json=obs(athlete="a2", attempts=4, completions=3)).json()
    b = client.post(f"{REC}/{rec}/performance", json=obs(athlete="a2", attempts=4, completions=4, supersedes=b0["id"], correction_reason="recount")).json()
    pkg = read_package(client.get(f"{REC}/{rec}/package.zip").content)
    assert {o.id for o in pkg.performance} == {a["id"], b0["id"], b["id"]} and pkg.manifest["counts"]["performance_observations"] == 3   # same period start and creation time: order by id
    dest = Database(tmp_path / "dest.db")
    try:
        new_id = import_package(dest, pkg, T0 + timedelta(days=1)).recording_id
    finally:
        dest.close()
    db = Database(tmp_path / "dest.db"); set_database(db)
    try:
        c = TestClient(create_app())
        got = {o["source_entry_id"]: o for o in c.get(f"{REC}/{new_id}/performance").json()}
        new_ctx = c.get(f"{REC}/{new_id}/context").json()[0]
        assert got[a["id"]]["coach_context_id"] == new_ctx["id"] != ctx["id"] and got[a["id"]]["origin"] == "source_engine"
        assert got[b["id"]]["completions"] == 4 and got[b["id"]]["recording_id"] == new_id
        assert got[b["id"]]["supersedes"] == got[b0["id"]]["id"] != b0["id"], "the correction chain is re-pointed to the imported ids"
    finally:
        db.close(); set_database(None)
    path = tmp_path / "test.db"
    isolated_db.close()
    lines = []
    assert local_data.main(["remove-athlete", "a1", "--database", str(path), "--execute", "--confirm", "a1"], out=lines.append) == 0, lines
    con = sqlite3.connect(path)
    try:
        assert [r[0] for r in con.execute("SELECT athlete_id FROM recording_performance")] == ["a2", "a2"]
    finally:
        con.close()


def test_coach_context_evidence_is_never_counted_as_performance(client, rec, monkeypatch):
    rubric(monkeypatch)
    client.post(f"{REC}/{rec}/context", json={"athlete_id": "a1", "effective_from": z(0), "evidence": {"status": "recorded", "items": [{"kind": "leadership", "task": "organising the back line", "description": "fictional"}]},
                                              "source": "head_coach", "certainty": "confirmed"})
    rv = client.get(f"{REC}/{rec}/athletes/a1/performance-review").json()
    assert all(a["evidence"]["observations"] == 0 for a in rv["areas"].values()) and rv["composite"]["available"] is False
