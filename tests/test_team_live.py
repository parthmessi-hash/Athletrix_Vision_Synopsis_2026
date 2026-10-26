"""GET /teams/{team_id}/live: one snapshot of every registered athlete on a team with the newest retained reading per stream.

Membership is Athlete.team_id from the registry. Backend time is read once; samples after it are excluded; ages are
never negative; old samples are still shown with their age. All data is fictional.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.core import clock
from app.models import TeamLive

NOW = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
PHYS = "/api/v1/samples/physiological"
TRACK = "/api/v1/samples/tracking"
TRK = {"x_position": 1.0, "y_position": 2.0, "speed_mps": 3.0, "acceleration_mps2": 0.0, "tracking_confidence": 0.9}


def at(seconds: float) -> datetime:
    return NOW + timedelta(seconds=seconds)


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    monkeypatch.setattr(clock, "utc_now", lambda: NOW)


def register(client, athlete_id, team_id, jersey, name=None):
    r = client.post("/api/v1/athletes", json={"id": athlete_id, "name": name or athlete_id.upper(), "jersey_number": jersey, "sport": "fictional", "team_id": team_id})
    assert r.status_code == 201, r.text


def post(client, path, body):
    r = client.post(path, json=body)
    assert r.status_code == 200, r.text


def live(client, team_id="team-a"):
    r = client.get(f"/api/v1/teams/{team_id}/live")
    assert r.status_code == 200, r.text
    return r.json()


def test_unknown_team_has_no_athletes_and_a_captured_timestamp(client):
    body = live(client, "no-such-team")
    assert body == {"team_id": "no-such-team", "captured_at": "2026-10-25T12:00:00Z", "athletes": []}
    TeamLive.model_validate(body)


def test_membership_comes_from_the_registry_and_is_sorted_by_jersey_then_id(client):
    register(client, "zed", "team-a", 7)
    register(client, "amy", "team-a", 7)
    register(client, "bob", "team-a", 3)
    register(client, "cat", "team-b", 1)
    body = live(client)
    assert [(a["athlete"]["id"], a["athlete"]["jersey_number"]) for a in body["athletes"]] == [("bob", 3), ("amy", 7), ("zed", 7)]
    assert all(a["physiological"] is None and a["tracking"] is None and a["physiological_age_seconds"] is None and a["tracking_age_seconds"] is None for a in body["athletes"])
    assert [a["athlete"]["id"] for a in live(client, "team-b")["athletes"]] == ["cat"]
    assert client.get("/api/v1/athletes").json()[0]["id"] == "amy", "the plain roster listing is unchanged"


def test_latest_reading_per_stream_with_ages_relative_to_the_captured_instant(client):
    register(client, "a1", "team-a", 10)
    register(client, "a2", "team-a", 11)
    post(client, PHYS, {"athlete_id": "a1", "timestamp": at(-30).isoformat(), "source": "wearable", "device_id": "w-1", "heart_rate_bpm": 120})
    post(client, PHYS, {"athlete_id": "a1", "timestamp": at(-1.5).isoformat(), "source": "wearable", "device_id": "w-1", "heart_rate_bpm": 131})
    post(client, TRACK, {"athlete_id": "a1", "timestamp": at(-0.25).isoformat(), "source": "camera", "device_id": "c-1", **TRK, "speed_mps": 0.0})
    post(client, TRACK, {"athlete_id": "a2", "timestamp": at(-600).isoformat(), **TRK})  # an old reading, still retained
    body = live(client)
    a1, a2 = body["athletes"]
    assert a1["physiological"]["heart_rate_bpm"] == 131 and a1["physiological"]["source"] == "wearable" and a1["physiological_age_seconds"] == 1.5
    assert a1["tracking"]["speed_mps"] == 0.0 and a1["tracking"]["source"] == "camera" and a1["tracking_age_seconds"] == 0.25
    assert a2["physiological"] is None and a2["physiological_age_seconds"] is None
    assert a2["tracking"]["speed_mps"] == 3.0 and a2["tracking"]["source"] is None and a2["tracking_age_seconds"] == 600.0, "stale readings are shown with their age, distinct from no reading"


def test_samples_after_the_captured_instant_are_excluded_and_ages_never_negative(client):
    register(client, "a1", "team-a", 10)
    post(client, PHYS, {"athlete_id": "a1", "timestamp": at(-2).isoformat(), "source": "simulated", "device_id": "d", "heart_rate_bpm": 100})
    post(client, PHYS, {"athlete_id": "a1", "timestamp": at(3).isoformat(), "source": "simulated", "device_id": "d", "heart_rate_bpm": 101})  # within the 5 s skew allowance
    post(client, TRACK, {"athlete_id": "a1", "timestamp": at(4).isoformat(), **TRK})
    assert client.get("/api/v1/athletes/a1/physiological/latest").json()["heart_rate_bpm"] == 101, "the plain latest endpoint still returns the newest row"
    a1 = live(client)["athletes"][0]
    assert a1["physiological"]["heart_rate_bpm"] == 100 and a1["physiological_age_seconds"] == 2.0
    assert a1["tracking"] is None and a1["tracking_age_seconds"] is None
    body = live(client)
    assert all((x["physiological_age_seconds"] or 0) >= 0 and (x["tracking_age_seconds"] or 0) >= 0 for x in body["athletes"])


def test_sample_exactly_at_the_captured_instant_is_included_with_age_zero(client):
    register(client, "a1", "team-a", 1)
    post(client, TRACK, {"athlete_id": "a1", "timestamp": NOW.isoformat(), **TRK})
    a1 = live(client)["athletes"][0]
    assert a1["tracking_age_seconds"] == 0.0


def test_equivalent_offsets_and_athlete_separation(client):
    register(client, "a1", "team-a", 1)
    register(client, "b1", "team-b", 1)
    post(client, PHYS, {"athlete_id": "a1", "timestamp": "2026-10-25T13:59:59+02:00", "source": "simulated", "device_id": "d", "heart_rate_bpm": 99})   # 11:59:59Z
    post(client, PHYS, {"athlete_id": "b1", "timestamp": at(-1).isoformat(), "source": "simulated", "device_id": "d", "heart_rate_bpm": 77})
    a = live(client)["athletes"][0]
    assert a["physiological"]["heart_rate_bpm"] == 99 and a["physiological_age_seconds"] == 1.0 and a["physiological"]["timestamp"] == "2026-10-25T13:59:59+02:00"
    assert live(client, "team-b")["athletes"][0]["physiological"]["heart_rate_bpm"] == 77


def test_backend_time_is_read_once_per_request(client, monkeypatch):
    register(client, "a1", "team-a", 1)
    calls = []
    monkeypatch.setattr(clock, "utc_now", lambda: calls.append(1) or NOW)
    live(client)
    assert len(calls) == 1


def test_unregistered_athletes_with_samples_are_not_listed(client):
    post(client, PHYS, {"athlete_id": "ghost", "timestamp": at(-1).isoformat(), "source": "simulated", "device_id": "d", "heart_rate_bpm": 99})
    assert live(client, "team-a")["athletes"] == []
