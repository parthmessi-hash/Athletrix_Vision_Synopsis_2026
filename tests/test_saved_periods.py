"""Saved effort periods of stopped recordings: create, list, get; validation; isolation; restart persistence. All data is fictional."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core import clock
from app.db import Database, set_database
from app.main import create_app
from app.models import SavedPeriod

NOW = datetime(2026, 10, 26, 20, 0, 0, tzinfo=UTC)
REC = "/api/v1/recordings"


@pytest.fixture
def frozen(monkeypatch):
    state = {"now": NOW}
    monkeypatch.setattr(clock, "utc_now", lambda: state["now"])
    return state


def register(client, *ids):
    for n, a in enumerate(ids, start=1):
        assert client.post("/api/v1/athletes", json={"id": a, "name": f"Fictional {a}", "jersey_number": n, "sport": "fictional", "team_id": "t"}).status_code == 201


def stopped_recording(client, frozen, ids=("a1", "a2"), name="session"):
    rec = client.post(REC, json={"name": name, "athlete_ids": list(ids)}).json()["id"]
    frozen["now"] = frozen["now"] + timedelta(minutes=5)
    assert client.post(f"{REC}/{rec}/stop").status_code == 200
    return rec


def periods_url(rec, athlete="a1"):
    return f"{REC}/{rec}/athletes/{athlete}/periods"


def iso(seconds):
    return (NOW + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def test_save_list_get_with_exact_instants_and_labels_that_may_repeat(client, frozen):
    register(client, "a1", "a2")
    rec = stopped_recording(client, frozen)
    r = client.post(periods_url(rec), json={"label": "  second sprint ", "effort_start": "2026-10-26T22:00:10+02:00", "effort_end": iso(30)})
    assert r.status_code == 201, r.text
    body = r.json()
    SavedPeriod.model_validate(body)
    assert body["id"].startswith("per-20261026T200500Z-") and body["recording_id"] == rec and body["athlete_id"] == "a1" and body["label"] == "second sprint"
    assert body["effort_start"] == "2026-10-26T22:00:10+02:00" and body["effort_end"] == iso(30) and body["duration_seconds"] == 20.0, "instants kept exactly as sent, offsets included"
    assert body["created_at"] == "2026-10-26T20:05:00Z"
    second = client.post(periods_url(rec), json={"label": "second sprint", "effort_start": iso(0), "effort_end": iso(5)}).json()
    assert second["id"] != body["id"] and second["label"] == body["label"], "identical labels are allowed; the id identifies the period"
    listed = client.get(periods_url(rec)).json()
    assert [p["id"] for p in listed] == [second["id"], body["id"]], "ordered by start instant"
    assert client.get(f"{periods_url(rec)}/{body['id']}").json() == body
    assert client.get(f"{periods_url(rec)}/per-nope").status_code == 404


def test_validation_creates_nothing(client, frozen):
    register(client, "a1")
    rec = stopped_recording(client, frozen, ids=("a1",))
    bad = [
        ({"label": "   ", "effort_start": iso(0), "effort_end": iso(10)}, 422),
        ({"label": "x", "effort_start": "2026-10-26T20:00:00", "effort_end": iso(10)}, 422),       # naive
        ({"label": "x", "effort_start": "1761508800", "effort_end": iso(10)}, 422),                # epoch
        ({"label": "x", "effort_start": iso(10), "effort_end": iso(0)}, 422),                      # reversed
        ({"label": "x", "effort_start": iso(0), "effort_end": iso(0.5)}, 422),                     # too short
        ({"label": "x", "effort_start": iso(0), "effort_end": iso(121)}, 422),                     # too long
        ({"label": "x", "effort_start": iso(290), "effort_end": iso(310)}, 422),                   # ends after backend time (20:05:00)
        ({"label": "x" * 81, "effort_start": iso(0), "effort_end": iso(10)}, 422),
        ({"label": "x", "effort_start": iso(0)}, 422),
        ({"label": "x", "effort_start": iso(0), "effort_end": iso(10), "note": "no"}, 422),
    ]
    for body, code in bad:
        assert client.post(periods_url(rec), json=body).status_code == code, body
    assert client.get(periods_url(rec)).json() == []


def test_only_stopped_recordings_and_only_roster_athletes(client, frozen):
    register(client, "a1", "a2")
    active = client.post(REC, json={"name": "live", "athlete_ids": ["a1"]}).json()["id"]
    r = client.post(periods_url(active), json={"label": "x", "effort_start": iso(-20), "effort_end": iso(-10)})
    assert r.status_code == 409 and "still active" in r.json()["detail"]
    frozen["now"] = NOW + timedelta(minutes=1)
    client.post(f"{REC}/{active}/stop")
    assert client.post(periods_url(active, "a2"), json={"label": "x", "effort_start": iso(-20), "effort_end": iso(-10)}).status_code == 404, "a2 is not in this recording"
    assert client.post(periods_url("rec-nope"), json={"label": "x", "effort_start": iso(-20), "effort_end": iso(-10)}).status_code == 404
    assert client.get(periods_url(active, "a2")).status_code == 404
    assert client.post(periods_url(active), json={"label": "x", "effort_start": iso(-20), "effort_end": iso(-10)}).status_code == 201


def test_periods_are_isolated_by_recording_and_athlete_and_empty_phases_are_valid(client, frozen):
    register(client, "a1", "a2")
    rec1 = stopped_recording(client, frozen, name="first")
    rec2 = stopped_recording(client, frozen, name="second")
    p1 = client.post(periods_url(rec1, "a1"), json={"label": "one", "effort_start": iso(0), "effort_end": iso(10)}).json()
    p2 = client.post(periods_url(rec1, "a2"), json={"label": "one", "effort_start": iso(0), "effort_end": iso(10)}).json()
    p3 = client.post(periods_url(rec2, "a1"), json={"label": "one", "effort_start": iso(0), "effort_end": iso(10)}).json()
    assert [p["id"] for p in client.get(periods_url(rec1, "a1")).json()] == [p1["id"]]
    assert [p["id"] for p in client.get(periods_url(rec1, "a2")).json()] == [p2["id"]]
    assert [p["id"] for p in client.get(periods_url(rec2, "a1")).json()] == [p3["id"]]
    assert client.get(f"{periods_url(rec2, 'a1')}/{p1['id']}").status_code == 404, "another recording's period id is not reachable here"
    assert client.get(f"{periods_url(rec1, 'a2')}/{p1['id']}").status_code == 404
    # reviewing a saved period whose phases hold no samples is valid and clearly empty
    review = client.get(f"{REC}/{rec1}/athletes/a1/effort-review", params={"effort_start": p1["effort_start"], "effort_end": p1["effort_end"]}).json()
    assert review["movement"]["sample_count"] == 0 and review["movement"]["estimated_distance_m"] is None
    assert all(review["heart_rate"][n]["sample_count"] == 0 and review["heart_rate"][n]["heart_rate_mean_bpm"] is None for n in ("before", "during", "after"))
    assert review["session_availability"]["movement"]["recorded_count"] == 0


def test_saved_periods_survive_a_restart(tmp_path, frozen):
    path = tmp_path / "periods.db"
    db = Database(path); set_database(db)
    with TestClient(create_app()) as c:
        register(c, "a1")
        rec = stopped_recording(c, frozen, ids=("a1",))
        saved = [c.post(periods_url(rec), json={"label": f"period {i}", "effort_start": iso(10 * i), "effort_end": iso(10 * i + 8)}).json() for i in range(3)]
    db.close(); set_database(None)
    db = Database(path); set_database(db)
    try:
        with TestClient(create_app()) as c:
            assert c.get(periods_url(rec)).json() == saved, "same ids, labels and exact instants after the restart"
            assert c.get(f"{periods_url(rec)}/{saved[2]['id']}").json()["effort_start"] == iso(20)
    finally:
        db.close(); set_database(None)
