"""Explicit local recordings: start, list, inspect, stop; validation, the single-active rule, roster snapshots and restarts.

Starting records future accepted ingestion only (nothing is copied from the rolling history). All data is fictional.
"""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core import clock
from app.db import Database, set_database
from app.main import create_app
from app.models import Recording, RecordingDetail

NOW = datetime(2026, 10, 26, 20, 0, 0, tzinfo=UTC)
REC = "/api/v1/recordings"
PHYS = "/api/v1/samples/physiological"


@pytest.fixture
def frozen(monkeypatch):
    state = {"now": NOW}
    monkeypatch.setattr(clock, "utc_now", lambda: state["now"])
    return state


def register(client, *ids):
    for n, athlete_id in enumerate(ids, start=1):
        r = client.post("/api/v1/athletes", json={"id": athlete_id, "name": f"Fictional {athlete_id}", "jersey_number": n, "sport": "fictional", "team_id": "team-x"})
        assert r.status_code == 201, r.text


def start(client, name="Tuesday practice", ids=("a1", "a2"), expect=201):
    r = client.post(REC, json={"name": name, "athlete_ids": list(ids)})
    assert r.status_code == expect, r.text
    return r.json()


# ---- start -------------------------------------------------------------------------------------------------------

def test_start_creates_an_active_recording_with_a_roster_snapshot(client, frozen):
    register(client, "a1", "a2", "a3")
    body = start(client, ids=("a2", "a1"))
    assert body["id"].startswith("rec-20261026T200000Z-") and len(body["id"]) == len("rec-20261026T200000Z-") + 6
    assert body["status"] == "active" and body["started_at"] == "2026-10-26T20:00:00Z" and body["stopped_at"] is None
    assert body["name"] == "Tuesday practice" and body["athlete_ids"] == ["a2", "a1"], "selection order is kept"
    assert [a["id"] for a in body["athletes"]] == ["a2", "a1"] and body["athletes"][0]["name"] == "Fictional a2" and body["athletes"][0]["jersey_number"] == 2
    Recording.model_validate(body)
    assert client.get(f"{REC}/active").json() == {"recording": body}


def test_start_validates_the_whole_selection_before_creating_anything(client, frozen):
    register(client, "a1")
    r = client.post(REC, json={"name": "x", "athlete_ids": ["a1", "ghost", "nobody"]})
    assert r.status_code == 422 and "ghost, nobody" in r.json()["detail"]
    assert client.get(REC).json() == [] and client.get(f"{REC}/active").json() == {"recording": None}, "nothing was created"
    for bad in ({"name": "   ", "athlete_ids": ["a1"]}, {"name": "x", "athlete_ids": []}, {"name": "x", "athlete_ids": ["a1", "a1"]}, {"name": "x", "athlete_ids": [" "]},
                {"name": "x"}, {"athlete_ids": ["a1"]}, {"name": "x", "athlete_ids": ["a1"], "auto": True}, {"name": "y" * 121, "athlete_ids": ["a1"]}):
        assert client.post(REC, json=bad).status_code == 422, bad
    assert client.get(REC).json() == []


def test_only_one_recording_can_be_active(client, frozen):
    register(client, "a1", "a2")
    first = start(client, ids=("a1",))
    r = client.post(REC, json={"name": "second", "athlete_ids": ["a2"]})
    assert r.status_code == 409 and first["id"] in r.json()["detail"] and "Tuesday practice" in r.json()["detail"]
    assert len(client.get(REC).json()) == 1
    frozen["now"] = NOW + timedelta(minutes=30)
    assert client.post(f"{REC}/{first['id']}/stop").status_code == 200
    second = start(client, name="second", ids=("a2",))
    assert second["status"] == "active" and second["id"] != first["id"]


def test_the_database_itself_refuses_a_second_active_row(isolated_db):
    with isolated_db.transaction() as c:
        c.execute("INSERT INTO recordings (id, name, started_at, stopped_at) VALUES ('r1', 'one', '2026-10-26T20:00:00.000000+00:00', NULL)")
        c.execute("INSERT INTO recordings (id, name, started_at, stopped_at) VALUES ('r0', 'old', '2026-10-26T19:00:00.000000+00:00', '2026-10-26T19:30:00.000000+00:00')")
    with pytest.raises(sqlite3.IntegrityError):
        with isolated_db.transaction() as c:
            c.execute("INSERT INTO recordings (id, name, started_at, stopped_at) VALUES ('r2', 'two', '2026-10-26T20:01:00.000000+00:00', NULL)")


# ---- list, inspect, stop ---------------------------------------------------------------------------------------------

def test_list_is_newest_first_and_detail_reports_recorded_counts(client, frozen):
    register(client, "a1", "a2")
    first = start(client, name="first", ids=("a1",))
    frozen["now"] = NOW + timedelta(minutes=1)
    client.post(f"{REC}/{first['id']}/stop")
    frozen["now"] = NOW + timedelta(minutes=2)
    second = start(client, name="second", ids=("a1", "a2"))
    assert [r["name"] for r in client.get(REC).json()] == ["second", "first"]
    detail = client.get(f"{REC}/{second['id']}").json()
    RecordingDetail.model_validate(detail)
    assert detail["status"] == "active" and [e["athlete"]["id"] for e in detail["recorded"]] == ["a1", "a2"]
    assert all(e["physiological"] == {"sample_count": 0, "earliest_timestamp": None, "latest_timestamp": None} and e["tracking"]["sample_count"] == 0 for e in detail["recorded"])
    assert client.get(f"{REC}/rec-nope").status_code == 404


def test_stop_is_idempotent_and_unknown_ids_are_404(client, frozen):
    register(client, "a1")
    rec = start(client, ids=("a1",))
    frozen["now"] = NOW + timedelta(minutes=10)
    stopped = client.post(f"{REC}/{rec['id']}/stop").json()
    assert stopped["status"] == "stopped" and stopped["stopped_at"] == "2026-10-26T20:10:00Z" and stopped["started_at"] == rec["started_at"]
    frozen["now"] = NOW + timedelta(minutes=20)
    again = client.post(f"{REC}/{rec['id']}/stop").json()
    assert again == stopped, "a second stop returns the existing state, not a new stop time"
    assert client.get(f"{REC}/active").json() == {"recording": None}
    assert client.post(f"{REC}/rec-nope/stop").status_code == 404


def test_starting_does_not_copy_existing_rolling_history(client, frozen):
    register(client, "a1")
    for s in range(5):
        assert client.post(PHYS, json={"athlete_id": "a1", "timestamp": (NOW - timedelta(seconds=10 - s)).isoformat(), "source": "simulated", "device_id": "d", "heart_rate_bpm": 100}).status_code == 200
    rec = start(client, ids=("a1",))
    detail = client.get(f"{REC}/{rec['id']}").json()
    assert detail["recorded"][0]["physiological"]["sample_count"] == 0, "existing history stays in the rolling store only"
    assert len(client.get("/api/v1/athletes/a1/physiological/history").json()) == 5


def test_roster_snapshot_outlives_registry_changes(client, frozen):
    register(client, "a1")
    rec = start(client, ids=("a1",))
    from app.store.athletes import athlete_registry
    athlete_registry.clear()
    assert client.get("/api/v1/athletes").json() == []
    body = client.get(f"{REC}/{rec['id']}").json()
    assert body["athletes"][0] == {"id": "a1", "name": "Fictional a1", "jersey_number": 1, "sport": "fictional", "team_id": "team-x"}


# ---- restart ---------------------------------------------------------------------------------------------------------------

def test_an_active_recording_survives_a_backend_restart_until_stopped(tmp_path, frozen):
    path = tmp_path / "recordings.db"
    db = Database(path); set_database(db)
    with TestClient(create_app()) as c:
        register(c, "a1")
        rec = start(c, ids=("a1",))
    db.close(); set_database(None)
    db = Database(path); set_database(db)
    try:
        with TestClient(create_app()) as c:
            assert c.get(f"{REC}/active").json()["recording"]["id"] == rec["id"], "still active after the restart"
            assert c.get(f"{REC}/{rec['id']}").json()["athletes"][0]["id"] == "a1"
            frozen["now"] = NOW + timedelta(hours=1)
            assert c.post(f"{REC}/{rec['id']}/stop").json()["stopped_at"] == "2026-10-26T21:00:00Z"
            assert c.get(f"{REC}/active").json() == {"recording": None}
    finally:
        db.close(); set_database(None)
