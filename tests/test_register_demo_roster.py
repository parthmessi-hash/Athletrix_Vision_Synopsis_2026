"""Demo roster registration script, exercised against the app via the test client."""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "register_demo_roster.py"
spec = importlib.util.spec_from_file_location("register_demo_roster", SCRIPT)
roster = importlib.util.module_from_spec(spec)
spec.loader.exec_module(roster)

ATHLETES = "/api/v1/athletes"


def outcomes(results):
    return {r.athlete_id: r.outcome for r in results}


def test_roster_is_two_fictional_athletes_with_required_fields():
    assert [a["id"] for a in roster.DEMO_ATHLETES] == ["athlete-1", "athlete-2"]
    for a in roster.DEMO_ATHLETES:
        assert set(a) == {"id", "name", "jersey_number", "sport", "team_id"}
    assert len({a["sport"] for a in roster.DEMO_ATHLETES}) == 2  # multi-sport demo


def test_first_run_registers_both(client):
    results = roster.register_roster(client)
    assert outcomes(results) == {"athlete-1": "registered", "athlete-2": "registered"}
    assert roster.exit_code(results) == 0
    listed = client.get(ATHLETES).json()
    assert [a["id"] for a in listed] == ["athlete-1", "athlete-2"]
    assert listed == roster.DEMO_ATHLETES  # sorted by id already


def test_second_run_leaves_matching_athletes_unchanged(client):
    roster.register_roster(client)
    results = roster.register_roster(client)
    assert outcomes(results) == {"athlete-1": "unchanged", "athlete-2": "unchanged"}
    assert roster.exit_code(results) == 0
    assert len(client.get(ATHLETES).json()) == 2


def test_conflict_is_reported_and_not_overwritten(client):
    different = {**roster.DEMO_ATHLETES[0], "jersey_number": 99, "name": "Someone Else"}
    assert client.post(ATHLETES, json=different).status_code == 201
    results = roster.register_roster(client)
    by_id = {r.athlete_id: r for r in results}
    assert by_id["athlete-1"].outcome == "conflict"
    assert "jersey_number" in by_id["athlete-1"].detail and "name" in by_id["athlete-1"].detail
    assert by_id["athlete-2"].outcome == "registered"
    assert roster.exit_code(results) == 1
    assert client.get(f"{ATHLETES}/athlete-1").json() == different  # untouched


def test_registration_does_not_affect_sample_ingest(client, physiological_payload):
    roster.register_roster(client)
    unregistered = {**physiological_payload, "athlete_id": "athlete-9"}
    assert client.post("/api/v1/samples/physiological", json=unregistered).status_code == 200
