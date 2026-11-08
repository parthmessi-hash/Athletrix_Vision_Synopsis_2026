"""Blind reviewer annotation sheets: separate questions, comparison of two independent FICTIONAL reviewers, adjudication
that never changes the originals, and import into the engine's manual evidence (no automatic recognition)."""

import copy
import json
import sys
from pathlib import Path

import pytest

from app.core import annotation as an

ROOT = Path(__file__).resolve().parents[1]
EX = ROOT / "scripts" / "examples" / "annotation"
sys.path.insert(0, str(ROOT / "scripts"))
import annotate  # noqa: E402


def load(name):
    return json.loads((EX / name).read_text())


def test_examples_cover_the_required_cases_and_validate():
    a = load("fictional_reviewer_a.json")
    assert an.validate(a, require_labels=True) == [] and an.validate(load("fictional_moments_blank.json")) == []
    m = {x["id"]: x["labels"] for x in a["moments"]}
    assert m["m1"]["execution_pass"]["completions"] == 1 and m["m1"]["decision"]["judgement"] == "poor", "a completed pass judged a poor choice"
    assert m["m2"]["execution_pass"]["completions"] == 0 and m["m2"]["decision"]["judgement"] == "good", "an unsuccessful pass judged a reasonable choice"
    assert "right back" in a["moments"][2]["criterion"]["positioning"], "a role-specific positioning expectation"
    assert m["m5"]["decision"] == {**m["m5"]["decision"], "assessment": "unable_to_assess", "unable_reason": "off_camera"}, "off camera: cannot be assessed"
    assert a["moments"][3]["active_instruction"]["exception"], "an instruction with a stated exception"


def test_comparison_counts_assessable_moments_and_lists_every_disagreement():
    r = an.compare(load("fictional_reviewer_a.json"), load("fictional_reviewer_b.json"))
    pq = r["per_question"]
    assert pq["execution_pass"] == {"moments_asked": 2, "unlabelled_by_either": 0, "same_assessability": 2, "both_assessed": 2, "agreed_where_both_assessed": 2, "agreement": 1.0}
    assert pq["decision"]["both_assessed"] == 2 and pq["decision"]["agreed_where_both_assessed"] == 1, "m5 decision: both unable (agree on assessability), not counted as agreement"
    assert pq["positioning"]["same_assessability"] == 1 and pq["positioning"]["both_assessed"] == 1
    assert {(d["moment"], d["question"]) for d in r["disagreements"]} == {("m2", "decision"), ("m3", "positioning"), ("m5", "positioning"), ("m4", "tactical")}
    assert r["fictional"] and "not accuracy" in r["note"]


def test_comparison_refuses_different_moments_or_one_reviewer_twice():
    a, b = load("fictional_reviewer_a.json"), load("fictional_reviewer_b.json")
    moved = copy.deepcopy(b); moved["moments"][0]["to_frame"] += 1
    with pytest.raises(an.SheetError, match="same moments"):
        an.compare(a, moved)
    same = copy.deepcopy(b); same["reviewer"] = a["reviewer"]
    with pytest.raises(an.SheetError, match="same reviewer"):
        an.compare(a, same)


def test_blank_copies_hide_labels_and_adjudication_keeps_the_originals(tmp_path):
    a, b = load("fictional_reviewer_a.json"), load("fictional_reviewer_b.json")
    before = (an.sheet_sha256(a), an.sheet_sha256(b))
    assert all(m["labels"] == {} for m in an.blank(a, "reviewer C")["moments"]), "a reviewer never receives someone else's labels"
    adj = an.adjudicate(a, b, load("fictional_adjudication_decisions.json")["decisions"], "fictional adjudicator")
    assert (an.sheet_sha256(a), an.sheet_sha256(b)) == before and adj["adjudication"]["from_sheets_sha256"] == list(before)
    assert sorted(adj["adjudication"]["unresolved"]) == ["m3:positioning", "m5:positioning"], "disagreements without a decision stay unresolved, not guessed"
    m = {x["id"]: x["labels"] for x in adj["moments"]}
    assert m["m4"]["tactical"]["judgement"] == "followed" and m["m1"]["decision"]["judgement"] == "poor"
    lines = []
    out = tmp_path / "adj.json"
    code = annotate.main(["adjudicate", str(EX / "fictional_reviewer_a.json"), str(EX / "fictional_reviewer_b.json"), "--decisions", str(EX / "fictional_adjudication_decisions.json"),
                          "--adjudicator", "fictional adjudicator", "--out", str(out)], out=lines.append)
    assert code == 0 and "2 unresolved" in lines[-1]


def test_import_into_the_engine_keeps_questions_apart(client, tmp_path, monkeypatch):
    from datetime import UTC, datetime, timedelta
    from app.core import clock
    now = {"t": datetime(2026, 11, 7, 10, tzinfo=UTC)}
    monkeypatch.setattr(clock, "utc_now", lambda: now["t"])
    client.post("/api/v1/athletes", json={"id": "f01", "name": "Fictional f01", "jersey_number": 8, "sport": "soccer", "team_id": "t"})
    for aid, n in (("f02", 4), ("f03", 2), ("f04", 7)):
        client.post("/api/v1/athletes", json={"id": aid, "name": f"Fictional {aid}", "jersey_number": n, "sport": "soccer", "team_id": "t"})
    rec = client.post("/api/v1/recordings", json={"name": "FICTIONAL", "athlete_ids": ["f01", "f02", "f03", "f04"]}).json()
    now["t"] += timedelta(minutes=3)
    client.post(f"/api/v1/recordings/{rec['id']}/stop")
    a = load("fictional_reviewer_a.json")
    a["clip"]["frame0_utc"] = rec["started_at"]
    p = tmp_path / "a.json"; p.write_text(json.dumps(a))
    lines = []
    assert annotate.main(["import", str(p), "--recording", rec["id"], "--engine", "http://testserver"], out=lines.append, client=client) == 0, lines
    obs = client.get(f"/api/v1/recordings/{rec['id']}/performance").json()
    m1 = [o for o in obs if o["description"].startswith("moment m1")]
    assert {(o["kind"], o.get("completions"), o.get("rated_poor")) for o in m1} == {("pass", 1, None), ("decision", None, 1)}, "execution and decision stay separate"
    m5 = next(o for o in obs if o["description"] == "moment m5 (decision)")
    assert m5["assessment"] == "unable_to_assess" and m5["unable_reason"] == "off_camera" and m5["reference_label"] is True and m5["annotator"] == "fictional reviewer A"
    m4 = next(o for o in obs if o["kind"] == "tactical_observation")
    assert m4["instruction_exception"].startswith("drop off") and m4["confidence"] == "low" and m4["rationale"]
