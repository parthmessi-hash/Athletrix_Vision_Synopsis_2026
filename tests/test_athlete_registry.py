"""Athlete registry store and its endpoints."""

from app.models import Athlete
from app.store.athletes import AthleteRegistry

ATHLETES = "/api/v1/athletes"


def make(athlete_id="ath-1", name="Alex Doe", number=10):
    return Athlete(id=athlete_id, name=name, jersey_number=number, sport="football", team_id="team-1")


# ---- registry unit tests -------------------------------------------------

def test_registry_empty():
    reg = AthleteRegistry()
    assert reg.list() == []
    assert reg.get("ath-1") is None


def test_registry_add_and_get():
    reg = AthleteRegistry()
    assert reg.add(make()) is True
    assert reg.get("ath-1").name == "Alex Doe"


def test_registry_duplicate_keeps_original():
    reg = AthleteRegistry()
    reg.add(make(name="Original"))
    assert reg.add(make(name="Imposter")) is False
    assert reg.get("ath-1").name == "Original"


def test_registry_list_sorted_by_id():
    reg = AthleteRegistry()
    for i in ("b", "c", "a"):
        reg.add(make(athlete_id=i))
    assert [a.id for a in reg.list()] == ["a", "b", "c"]


# ---- endpoint tests ------------------------------------------------------

def test_list_empty(client):
    r = client.get(ATHLETES)
    assert r.status_code == 200
    assert r.json() == []


def test_register_returns_201_and_echo(client, athlete_payload):
    r = client.post(ATHLETES, json=athlete_payload)
    assert r.status_code == 201
    assert r.json() == athlete_payload


def test_register_then_get(client, athlete_payload):
    client.post(ATHLETES, json=athlete_payload)
    r = client.get(f"{ATHLETES}/{athlete_payload['id']}")
    assert r.status_code == 200
    assert r.json() == athlete_payload


def test_get_unregistered_404(client):
    r = client.get(f"{ATHLETES}/ghost")
    assert r.status_code == 404
    assert "ghost" in r.json()["detail"]


def test_duplicate_register_409_without_overwrite(client, athlete_payload):
    assert client.post(ATHLETES, json=athlete_payload).status_code == 201
    r = client.post(ATHLETES, json={**athlete_payload, "name": "Someone Else", "jersey_number": 99})
    assert r.status_code == 409
    assert athlete_payload["id"] in r.json()["detail"]
    assert client.get(f"{ATHLETES}/{athlete_payload['id']}").json() == athlete_payload
    assert len(client.get(ATHLETES).json()) == 1


def test_list_sorted_by_id(client, athlete_payload):
    for i, athlete_id in enumerate(["ath-9", "ath-10", "ath-1"]):
        client.post(ATHLETES, json={**athlete_payload, "id": athlete_id, "jersey_number": i})
    assert [a["id"] for a in client.get(ATHLETES).json()] == ["ath-1", "ath-10", "ath-9"]


def test_invalid_register_422_and_not_stored(client, athlete_payload):
    r = client.post(ATHLETES, json={**athlete_payload, "jersey_number": -1})
    assert r.status_code == 422
    assert client.get(ATHLETES).json() == []


def test_validate_does_not_register(client, athlete_payload):
    assert client.post(f"{ATHLETES}/validate", json=athlete_payload).status_code == 200
    assert client.get(ATHLETES).json() == []
    assert client.get(f"{ATHLETES}/{athlete_payload['id']}").status_code == 404


def test_registry_cleared_between_tests(client):
    assert client.get(ATHLETES).json() == []


def test_sample_ingest_does_not_require_registration(client, physiological_payload, tracking_payload):
    assert client.get(f"{ATHLETES}/{physiological_payload['athlete_id']}").status_code == 404
    assert client.post("/api/v1/samples/physiological", json=physiological_payload).status_code == 200
    assert client.post("/api/v1/samples/tracking", json=tracking_payload).status_code == 200
    assert client.get(f"{ATHLETES}/ath-1/physiological/latest").status_code == 200
    assert client.get(f"{ATHLETES}/ath-1/tracking/latest").status_code == 200
