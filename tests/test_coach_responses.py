"""Coach / operator responses and lineup changes on the FICTIONAL demo recording: idempotent saves, the recommendation
pinned from a saved review, roster and constraint checks, no silent decrement, package remapping and removal."""

import importlib.util
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import review as review_api
from app.core.config import Settings
from app.core.recording_package import read_package
from app.db import Database, set_database
from app.main import create_app
from app.store.recording_import import import_package

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("product_demo_resp", ROOT / "scripts" / "product_demo.py")
demo = importlib.util.module_from_spec(spec); spec.loader.exec_module(demo)
spec2 = importlib.util.spec_from_file_location("local_data_resp", ROOT / "scripts" / "local_data.py")
local_data = importlib.util.module_from_spec(spec2); spec2.loader.exec_module(local_data)
SETTINGS = Settings(physiological_index_profile_path=str(demo.PROFILE_FILES["PHYSIOLOGICAL_INDEX_PROFILE_PATH"]), substitution_profile_path=str(demo.PROFILE_FILES["SUBSTITUTION_PROFILE_PATH"]),
                    performance_rubric_path=str(demo.PROFILE_FILES["PERFORMANCE_RUBRIC_PATH"]))


@pytest.fixture(scope="module")
def package():
    return read_package(demo.build())


@pytest.fixture
def rid(isolated_db, package, monkeypatch):
    monkeypatch.setattr(review_api, "get_settings", lambda: SETTINGS)
    return import_package(isolated_db, package, datetime(2026, 11, 2, tzinfo=UTC)).recording_id


def saved_review(client, rid, athlete="p02"):
    return client.post(f"/api/v1/recordings/{rid}/athletes/{athlete}/reviews", json={"kind": "substitution", "at": demo.z(demo.T_MAIN), "knowledge": "as_entered"}).json()


def lineup(out_id, in_id, token, role=None, **kw):
    return {"athlete_id": out_id, "kind": "lineup_change", "client_token": token, "simulated": True,
            "lineup": {"outgoing_athlete_id": out_id, "incoming_athlete_id": in_id, "effective_from": demo.z(demo.T_MAIN + 60), **({"incoming_role": role} if role else {}), **kw}}


def test_responses_pin_the_saved_recommendation_and_are_idempotent(client, rid):
    url = f"/api/v1/recordings/{rid}/responses"
    assert client.post(url, json={"athlete_id": "p02", "kind": "acknowledged", "client_token": "tok-00001"}).status_code == 422, "a response names the saved review"
    rv = saved_review(client, rid)
    body = {"athlete_id": "p02", "kind": "dismiss", "client_token": "tok-00002", "saved_review_id": rv["id"], "reason": "fictional: wants one more minute", "simulated": True}
    first = client.post(url, json=body)
    again = client.post(url, json=body)
    assert first.status_code == 201 and again.status_code == 200 and again.json()["id"] == first.json()["id"], "a retry returns the stored response"
    assert client.post(url, json={**body, "reason": "something else"}).status_code == 409, "a token is never reused for a different response"
    rec = first.json()["recommendation"]
    assert rec == {"saved_review_id": rv["id"], "outcome": rv["result"]["outcome"], "review_at": demo.z(demo.T_MAIN), "knowledge": "as_entered", "profiles": rv["result"]["profiles"]}
    assert "does not show the recommendation was right or wrong" in first.json()["note"]
    assert len(client.get(url).json()) == 1
    team = client.get(f"/api/v1/recordings/{rid}/roster/effective", params={"at": demo.z(demo.T_MAIN + 120)}).json()["team"]
    assert team["substitutions_remaining"] == 3, "a dismissed suggestion uses no substitution"


def test_lineup_changes_respect_roster_and_constraints(client, rid):
    url = f"/api/v1/recordings/{rid}/responses"
    r = client.post(url, json=lineup("p02", "b04", "tok-00010"))
    assert r.status_code == 409 and "do not bring on b04" in r.json()["detail"]
    r = client.post(url, json=lineup("p02", "b03", "tok-00011"))
    assert r.status_code == 409 and "do not include 'central midfielder'" in r.json()["detail"]
    assert client.post(url, json=lineup("b01", "b02", "tok-00012")).status_code == 409, "the outgoing athlete must be on the field"
    ok = client.post(url, json=lineup("p02", "b01", "tok-00013"))
    assert ok.status_code == 201 and len(ok.json()["roster_entry_ids"]) == 3 and ok.json()["warnings"] == [] and ok.json()["simulated"] is True
    st = client.get(f"/api/v1/recordings/{rid}/roster/effective", params={"at": demo.z(demo.T_MAIN + 120)}).json()
    assert st["athletes"]["p02"]["status"] == "unavailable" and st["athletes"]["b01"]["status"] == "on_field" and st["athletes"]["b01"]["current_role"] == "central midfielder"
    assert st["team"]["substitutions_remaining"] == 2 and st["team"]["do_not_bring_on"][0]["athlete_id"] == "b04", "the other constraints are carried over"
    before = client.get(f"/api/v1/recordings/{rid}/roster/effective", params={"at": demo.z(demo.T_MAIN)}).json()
    assert before["athletes"]["p02"]["status"] == "on_field", "history before the change is unchanged"
    r = client.post(url, json=lineup("p02", "b02", "tok-00014"))
    assert r.status_code == 409 and "not recorded on the field" in r.json()["detail"]


def test_responses_travel_in_packages_with_remapped_references_and_go_with_removed_athletes(client, rid, isolated_db, tmp_path):
    rv = saved_review(client, rid)
    url = f"/api/v1/recordings/{rid}/responses"
    client.post(url, json={"athlete_id": "p02", "kind": "acknowledged", "client_token": "tok-00020", "saved_review_id": rv["id"], "simulated": True})
    client.post(url, json=lineup("p02", "b01", "tok-00021"))
    pkg = read_package(client.get(f"/api/v1/recordings/{rid}/package.zip").content)
    assert pkg.manifest["format_version"] == 7 and len(pkg.responses) == 2
    dest = Database(tmp_path / "dest.db")
    new_id = import_package(dest, pkg, datetime(2026, 11, 3, tzinfo=UTC)).recording_id
    dest.close()
    db = Database(tmp_path / "dest.db"); set_database(db)
    try:
        c = TestClient(create_app())
        got = c.get(f"/api/v1/recordings/{new_id}/responses").json()
        reviews = {x["id"] for x in c.get(f"/api/v1/recordings/{new_id}/reviews").json()}
        roster = {x["id"] for x in c.get(f"/api/v1/recordings/{new_id}/roster").json()}
        ack, change = got
        assert ack["saved_review_id"] in reviews and ack["recommendation"]["saved_review_id"] == ack["saved_review_id"] != rv["id"] and ack["origin"] == "source_engine"
        assert set(change["roster_entry_ids"]) <= roster
    finally:
        db.close(); set_database(None)
    path = tmp_path / "test.db"
    isolated_db.close()
    lines = []
    assert local_data.main(["remove-athlete", "b01", "--database", str(path), "--execute", "--confirm", "b01"], out=lines.append) == 0, lines
    con = sqlite3.connect(path)
    try:
        assert con.execute("SELECT COUNT(*) FROM recording_responses").fetchone()[0] == 0, "the acknowledgement referred to a removed saved review; the change brought b01 on"
    finally:
        con.close()
