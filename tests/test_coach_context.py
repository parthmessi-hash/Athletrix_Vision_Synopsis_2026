"""Coach-context records: validation, effective-time selection, persistence, package round trip and removal. Fictional data."""

import importlib.util
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core import clock
from app.core.recording_package import read_package
from app.db import Database, set_database
from app.main import create_app
from app.models.context import ContextCreate
from app.store.recording_import import import_package

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("local_data_cli_context", ROOT / "scripts" / "local_data.py")
local_data = importlib.util.module_from_spec(spec); spec.loader.exec_module(local_data)

T0 = datetime(2026, 10, 29, 15, 0, 0, tzinfo=UTC)
REC = "/api/v1/recordings"


def z(s):
    return (T0 + timedelta(seconds=s)).isoformat().replace("+00:00", "Z")


def team(s, formation="4-3-3", **kw):
    return {"effective_from": z(s), "formation": {"status": "recorded", "value": formation}, "source": "head_coach", "certainty": "confirmed", **kw}


def player(athlete, s, role="left winger", **kw):
    return {"athlete_id": athlete, "effective_from": z(s), "role": {"status": "recorded", "value": role}, "source": "assistant_coach", "certainty": "probable", **kw}


@pytest.fixture
def frozen(monkeypatch):
    state = {"now": T0}
    monkeypatch.setattr(clock, "utc_now", lambda: state["now"])
    return state


@pytest.fixture
def rec(client, frozen):
    for n, a in enumerate(("a1", "a2", "a3"), start=1):
        assert client.post("/api/v1/athletes", json={"id": a, "name": f"Fictional {a}", "jersey_number": n, "sport": "soccer", "team_id": "t"}).status_code == 201
    rid = client.post(REC, json={"name": "fictional match", "athlete_ids": ["a1", "a2", "a3"]}).json()["id"]
    frozen["now"] = T0 + timedelta(seconds=3000)
    return rid


# ---- the model ---------------------------------------------------------------------------------------------------------

def test_every_item_has_an_explicit_status_and_no_numeric_score_exists():
    entry = ContextCreate.model_validate({"effective_from": z(0), "source": "analyst", "certainty": "uncertain"})
    assert entry.formation.status == entry.role.status == entry.partnerships.status == entry.evidence.status == "not_assessed"
    schema = json.dumps(ContextCreate.model_json_schema())
    assert '"type": "number"' not in schema and '"type": "integer"' not in schema, "context carries no ratings, bonuses or scores"
    for bad in ({"formation": {"status": "recorded"}}, {"formation": {"status": "unknown", "value": "4-4-2"}},
                {"evidence": {"status": "recorded", "items": []}}, {"partnerships": {"status": "not_assessed", "items": [{"partner_athlete_id": "a2", "note": "x"}]}}):
        with pytest.raises(ValidationError):
            ContextCreate.model_validate({"effective_from": z(0), "source": "analyst", "certainty": "uncertain", **bad})


def test_scope_rules_keep_team_and_athlete_items_apart():
    with pytest.raises(ValidationError, match="team-level entry cannot record role"):
        ContextCreate.model_validate({**team(0), "role": {"status": "recorded", "value": "striker"}})
    with pytest.raises(ValidationError, match="formation is team-level"):
        ContextCreate.model_validate({**player("a1", 0), "formation": {"status": "recorded", "value": "4-4-2"}})
    with pytest.raises(ValidationError, match="own partner"):
        ContextCreate.model_validate({**player("a1", 0), "partnerships": {"status": "recorded", "items": [{"partner_athlete_id": "a1", "note": "x"}]}})
    with pytest.raises(ValidationError, match="effective_to must be after"):
        ContextCreate.model_validate({**team(10), "effective_to": z(5)})


# ---- the API -------------------------------------------------------------------------------------------------------------

def test_entries_are_validated_against_the_recording(client, rec):
    assert client.post(f"{REC}/rec-missing/context", json=team(10)).status_code == 404
    assert client.post(f"{REC}/{rec}/context", json=player("ghost", 10)).status_code == 404
    bad_partner = {**player("a1", 10), "partnerships": {"status": "recorded", "items": [{"partner_athlete_id": "ghost", "note": "x"}]}}
    assert client.post(f"{REC}/{rec}/context", json=bad_partner).status_code == 404
    assert client.post(f"{REC}/{rec}/context", json=team(-5)).status_code == 422          # before the recording started
    assert client.post(f"{REC}/{rec}/context", json=team(3100)).status_code == 422        # after backend time (active recording)
    late_evidence = {**player("a1", 10), "evidence": {"status": "recorded", "items": [{"kind": "leadership", "task": "set pieces", "description": "organised the wall", "observed_at": z(-60)}]}}
    assert client.post(f"{REC}/{rec}/context", json=late_evidence).status_code == 422
    r = client.post(f"{REC}/{rec}/context", json=team(10))
    assert r.status_code == 201 and r.json()["origin"] == "this_engine" and r.json()["id"].startswith("ctx-") and r.json()["athlete_id"] is None


def test_effective_time_selection_corrections_expiry_and_separate_scopes(client, rec, frozen):
    post = lambda body: client.post(f"{REC}/{rec}/context", json=body).json()   # noqa: E731
    t1 = post(team(0, "4-3-3"))
    t2 = post(team(1200, "3-5-2", tactical_instruction={"status": "recorded", "value": "press high after the restart"}))
    frozen["now"] = T0 + timedelta(seconds=3001)
    t2_fix = post(team(1200, "3-4-3", note="correction: the coach changed it at half time"))   # same instant, created later: wins
    p1 = post(player("a1", 300, "left winger", effective_to=z(900), partnerships={"status": "recorded", "items": [{"partner_athlete_id": "a2", "note": "overlaps with a2 on the left"}]},
                     evidence={"status": "recorded", "items": [{"kind": "pressure_situation", "task": "defending a late corner", "description": "won the first header", "observed_at": z(800)}]}))
    eff = lambda at, athlete=None: client.get(f"{REC}/{rec}/context/effective", params={"at": z(at), **({"athlete_id": athlete} if athlete else {})}).json()   # noqa: E731
    assert eff(-1)["team"] is None
    assert eff(0)["team"]["id"] == t1["id"] and eff(1199)["team"]["id"] == t1["id"]
    assert eff(1200)["team"]["id"] == t2_fix["id"] and eff(2000)["team"]["formation"]["value"] == "3-4-3"
    e = eff(600, "a1")
    assert e["athlete"]["id"] == p1["id"] and e["team"]["id"] == t1["id"] and e["athlete"]["formation"]["status"] == "not_assessed"
    assert eff(900, "a1")["athlete"] is None, "effective_to is exclusive"
    assert eff(600, "a2")["athlete"] is None, "a partner mention is not an entry for the partner"
    assert "later-created entry" in e["rule"]
    listed = client.get(f"{REC}/{rec}/context").json()
    assert [x["id"] for x in listed] == [t1["id"], p1["id"], t2["id"], t2_fix["id"]]
    assert [x["id"] for x in client.get(f"{REC}/{rec}/context", params={"team": True}).json()] == [t1["id"], t2["id"], t2_fix["id"]]
    assert [x["id"] for x in client.get(f"{REC}/{rec}/context", params={"athlete_id": "a1"}).json()] == [p1["id"]]


def test_context_survives_a_restart_and_a_package_round_trip(client, rec, frozen, isolated_db, tmp_path):
    p = client.post(f"{REC}/{rec}/context", json=player("a1", 300, evidence={"status": "unknown"}, tactical_instruction={"status": "recorded", "value": "hold the width"})).json()
    t = client.post(f"{REC}/{rec}/context", json=team(0, match_label="U17 v Fictional FC")).json()
    client.post(f"{REC}/{rec}/stop")
    body = client.get(f"{REC}/{rec}/package.zip").content
    pkg = read_package(body)
    assert [e.id for e in pkg.context] == [t["id"], p["id"]] and pkg.manifest["counts"]["context_entries"] == 2
    dest = Database(tmp_path / "dest.db")
    try:
        new_id = import_package(dest, pkg, T0 + timedelta(days=1)).recording_id
    finally:
        dest.close()
    db = Database(tmp_path / "dest.db"); set_database(db)
    try:
        c = TestClient(create_app())
        got = c.get(f"{REC}/{new_id}/context").json()
        assert [g["source_entry_id"] for g in got] == [t["id"], p["id"]] and all(g["origin"] == "source_engine" and g["recording_id"] == new_id for g in got)
        strip = lambda e: {k: v for k, v in e.items() if k not in ("id", "recording_id", "origin", "source_entry_id")}   # noqa: E731
        assert [strip(g) for g in got] == [strip(t), strip(p)], "content, statuses, certainty and creation time unchanged"
        eff = c.get(f"{REC}/{new_id}/context/effective", params={"at": z(400), "athlete_id": "a1"}).json()
        assert eff["athlete"]["tactical_instruction"]["value"] == "hold the width" and eff["athlete"]["evidence"]["status"] == "unknown" and eff["team"]["match_label"] == "U17 v Fictional FC"
    finally:
        db.close(); set_database(None)


def test_removal_takes_context_with_the_athlete_or_recording_and_drops_partner_mentions(client, rec, frozen, isolated_db, tmp_path):
    client.post(f"{REC}/{rec}/context", json=team(0))
    client.post(f"{REC}/{rec}/context", json=player("a1", 10))
    keep = client.post(f"{REC}/{rec}/context", json=player("a2", 10, partnerships={"status": "recorded", "items": [{"partner_athlete_id": "a1", "note": "links with a1"}, {"partner_athlete_id": "a3", "note": "covers for a3"}]})).json()
    only = client.post(f"{REC}/{rec}/context", json=player("a3", 10, partnerships={"status": "recorded", "items": [{"partner_athlete_id": "a1", "note": "x"}]})).json()
    client.post(f"{REC}/{rec}/stop")
    path = tmp_path / "test.db"
    isolated_db.close()
    lines = []
    assert local_data.main(["remove-athlete", "a1", "--database", str(path), "--execute", "--confirm", "a1"], out=lines.append) == 0, lines
    con = sqlite3.connect(path)
    try:
        assert con.execute("SELECT COUNT(*) FROM recording_context WHERE athlete_id = 'a1'").fetchone()[0] == 0
        payloads = {r[0]: json.loads(r[1]) for r in con.execute("SELECT id, payload FROM recording_context WHERE athlete_id IS NOT NULL")}
        assert payloads[keep["id"]]["partnerships"] == {"status": "recorded", "items": [{"partner_athlete_id": "a3", "note": "covers for a3"}]}
        assert payloads[only["id"]]["partnerships"] == {"status": "unknown", "items": []}
        assert "a1" not in json.dumps(list(payloads.values()))
        assert con.execute("SELECT COUNT(*) FROM recording_context WHERE athlete_id IS NULL").fetchone()[0] == 1, "team-level context stays"
    finally:
        con.close()
    assert any("drop the partnership with a1" in line for line in lines) or "REMOVED" in lines[0]
    lines = []
    assert local_data.main(["remove-recording", rec, "--database", str(path), "--execute", "--confirm", rec], out=lines.append) == 0, lines
    con = sqlite3.connect(path)
    try:
        assert con.execute("SELECT COUNT(*) FROM recording_context").fetchone()[0] == 0
    finally:
        con.close()
