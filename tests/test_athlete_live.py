"""GET /athletes/{id}/live: a coherent single-athlete snapshot using the team overview's selection.

One captured instant; newest retained sample per stream at or before it; future-dated samples stay stored but are not
shown as current (with an explicit "newest ahead" figure); ages never negative; device statuses with explicit skew.
The plain latest endpoints keep their newest-stored semantics and registration stays optional. All data is fictional.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.core import clock
from app.models import AthleteLive

NOW = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
PHYS = "/api/v1/samples/physiological"
TRACK = "/api/v1/samples/tracking"
TRK = {"x_position": 1.0, "y_position": 2.0, "speed_mps": 3.0, "acceleration_mps2": 0.0, "tracking_confidence": 0.9}


def at(seconds: float) -> datetime:
    return NOW + timedelta(seconds=seconds)


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    monkeypatch.setattr(clock, "utc_now", lambda: NOW)


def register(client, athlete_id, team_id="team-a", jersey=1):
    assert client.post("/api/v1/athletes", json={"id": athlete_id, "name": athlete_id, "jersey_number": jersey, "sport": "fictional", "team_id": team_id}).status_code == 201


def post(client, path, body):
    r = client.post(path, json=body)
    assert r.status_code == 200, r.text


def live(client, athlete_id="a1"):
    r = client.get(f"/api/v1/athletes/{athlete_id}/live")
    assert r.status_code == 200, r.text
    return r.json()


def test_unregistered_and_unknown_ids_are_answered_not_404(client):
    body = live(client, "ghost")
    assert body["registered"] is False and body["athlete"] is None and body["physiological"] is None and body["tracking"] is None and body["devices"] == []
    assert body["captured_at"] == "2026-10-25T12:00:00Z"
    AthleteLive.model_validate(body)
    post(client, PHYS, {"athlete_id": "ghost", "timestamp": at(-1).isoformat(), "source": "simulated", "device_id": "d", "heart_rate_bpm": 100})
    body = live(client, "ghost")
    assert body["registered"] is False and body["physiological"]["heart_rate_bpm"] == 100 and body["physiological_age_seconds"] == 1.0, "ingestion never required registration and neither does the snapshot"
    assert client.get("/api/v1/athletes/ghost/physiological/history?limit=5").status_code == 200


def test_future_dated_sample_is_not_current_and_the_earlier_sample_is_shown_with_its_real_age(client):
    register(client, "a1")
    post(client, PHYS, {"athlete_id": "a1", "timestamp": at(-2.5).isoformat(), "source": "wearable", "device_id": "w", "heart_rate_bpm": 100})
    post(client, PHYS, {"athlete_id": "a1", "timestamp": at(3).isoformat(), "source": "wearable", "device_id": "w", "heart_rate_bpm": 101})   # within the 5 s skew allowance
    post(client, TRACK, {"athlete_id": "a1", "timestamp": at(4).isoformat(), **TRK})                                                           # only a future tracking sample
    body = live(client)
    assert body["physiological"]["heart_rate_bpm"] == 100 and body["physiological_age_seconds"] == 2.5
    assert body["physiological_newest_ahead_seconds"] == 3.0, "the stored newer sample is reported as ahead, not shown as current"
    assert body["tracking"] is None and body["tracking_age_seconds"] is None and body["tracking_newest_ahead_seconds"] == 4.0, "all-future stream: no eligible sample, with the ahead figure"
    # the plain latest endpoints keep their documented newest-stored semantics
    assert client.get("/api/v1/athletes/a1/physiological/latest").json()["heart_rate_bpm"] == 101
    assert client.get("/api/v1/athletes/a1/tracking/latest").json()["speed_mps"] == 3.0
    # once backend time reaches the sample, it becomes current with age 0 and nothing is ahead any more
    clock_later = at(3)
    import app.core.clock as c
    c_prev = c.utc_now
    c.utc_now = lambda: clock_later
    try:
        body = live(client)
        assert body["physiological"]["heart_rate_bpm"] == 101 and body["physiological_age_seconds"] == 0.0 and body["physiological_newest_ahead_seconds"] is None
        assert body["tracking"] is None and body["tracking_newest_ahead_seconds"] == 1.0
    finally:
        c.utc_now = c_prev


def test_no_stored_sample_has_no_ahead_figure_and_null_everything(client):
    register(client, "a1")
    body = live(client)
    assert body["physiological"] is None and body["physiological_newest_ahead_seconds"] is None and body["tracking_newest_ahead_seconds"] is None


def test_team_and_individual_snapshots_agree_at_the_same_captured_instant(client):
    register(client, "a1", jersey=1)
    register(client, "a2", jersey=2)
    post(client, PHYS, {"athlete_id": "a1", "timestamp": at(-1).isoformat(), "source": "wearable", "device_id": "w-1", "heart_rate_bpm": None})
    post(client, PHYS, {"athlete_id": "a1", "timestamp": at(2).isoformat(), "source": "wearable", "device_id": "w-1", "heart_rate_bpm": 140})   # ahead
    post(client, TRACK, {"athlete_id": "a1", "timestamp": at(-0.5).isoformat(), "source": "camera", "device_id": "c-1", **TRK, "speed_mps": 0.0})
    post(client, TRACK, {"athlete_id": "a2", "timestamp": at(-300).isoformat(), **TRK})
    post(client, "/api/v1/devices/status", {"device_id": "w-1", "timestamp": at(3).isoformat(), "connected": True, "battery_percent": 0, "signal_quality": 0.0})
    post(client, "/api/v1/devices/status", {"device_id": "c-1", "timestamp": at(-90).isoformat(), "connected": False, "battery_percent": None, "signal_quality": None})
    team = {row["athlete"]["id"]: row for row in client.get("/api/v1/teams/team-a/live").json()["athletes"]}
    for athlete_id in ("a1", "a2"):
        single = live(client, athlete_id)
        row = team[athlete_id]
        for key in ("physiological", "tracking", "physiological_age_seconds", "tracking_age_seconds", "physiological_newest_ahead_seconds", "tracking_newest_ahead_seconds", "devices"):
            assert single[key] == row[key], (athlete_id, key)
        assert single["registered"] is True and single["athlete"] == row["athlete"] and single["captured_at"] == "2026-10-25T12:00:00Z"
    a1 = live(client, "a1")
    assert a1["physiological"]["heart_rate_bpm"] is None and a1["physiological_newest_ahead_seconds"] == 2.0
    assert a1["tracking"]["speed_mps"] == 0.0 and a1["tracking_age_seconds"] == 0.5
    devs = {d["device_id"]: d for d in a1["devices"]}
    assert devs["w-1"]["status_age_seconds"] == 0.0 and devs["w-1"]["status_ahead_seconds"] == 3.0 and devs["w-1"]["status"]["battery_percent"] == 0
    assert devs["c-1"]["status_age_seconds"] == 90.0 and devs["c-1"]["status_ahead_seconds"] == 0.0 and devs["c-1"]["status"]["connected"] is False


def test_backend_time_is_read_once_and_the_snapshot_is_one_read(client, monkeypatch):
    register(client, "a1")
    calls = []
    monkeypatch.setattr(clock, "utc_now", lambda: calls.append(1) or NOW)
    live(client)
    assert len(calls) == 1
    from app.db import get_database
    db = get_database()
    depth = []
    real_read = db.read

    def counting_read():
        depth.append(1)
        return real_read()
    monkeypatch.setattr(db, "read", counting_read)
    live(client)
    assert depth and depth[0] == 1, "the endpoint opens the read context itself (store reads nest inside it)"
