"""Device statuses in the team overview: associated only through device ids reported by the newest samples.

A matching id is a reported association, not a verified identity. The newest status report per device is shown with
its age; an old report proves nothing about the current connection, and nothing is inferred from samples stopping.
All data is fictional.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.core import clock
from app.db import to_utc_iso

NOW = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
PHYS = "/api/v1/samples/physiological"
TRACK = "/api/v1/samples/tracking"
STATUS = "/api/v1/devices/status"
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


def phys(client, athlete_id, sec, device_id="w-1"):
    body = {"athlete_id": athlete_id, "timestamp": at(sec).isoformat(), "source": "wearable", "device_id": device_id, "heart_rate_bpm": 100}
    post(client, PHYS, body)


def track(client, athlete_id, sec, device_id=None):
    body = {"athlete_id": athlete_id, "timestamp": at(sec).isoformat(), **TRK}
    if device_id is not None:
        body["device_id"] = device_id
        body["source"] = "camera"
    post(client, TRACK, body)


def status(client, device_id, sec, connected=True, battery=50, signal=0.5):
    post(client, STATUS, {"device_id": device_id, "timestamp": at(sec).isoformat(), "connected": connected, "battery_percent": battery, "signal_quality": signal})


def devices(client, athlete_id="a1", team_id="team-a"):
    body = client.get(f"/api/v1/teams/{team_id}/live").json()
    return next(a for a in body["athletes"] if a["athlete"]["id"] == athlete_id)["devices"]


def test_different_device_ids_across_streams_give_two_entries_each_with_its_own_status(client):
    register(client, "a1")
    phys(client, "a1", -1, "w-1")
    track(client, "a1", -1, "c-1")
    status(client, "w-1", -3, True, 72, 0.6)
    status(client, "c-1", -10, False, None, None)
    d = devices(client)
    assert [x["device_id"] for x in d] == ["w-1", "c-1"]
    assert d[0]["streams"] == ["physiological"] and d[0]["status"]["connected"] is True and d[0]["status"]["battery_percent"] == 72 and d[0]["status_age_seconds"] == 3.0
    assert d[1]["streams"] == ["tracking"] and d[1]["status"]["connected"] is False and d[1]["status"]["battery_percent"] is None and d[1]["status"]["signal_quality"] is None and d[1]["status_age_seconds"] == 10.0


def test_shared_device_id_is_deduplicated_and_lists_both_streams(client):
    register(client, "a1")
    phys(client, "a1", -1, "unit-7")
    track(client, "a1", -2, "unit-7")
    status(client, "unit-7", -1.5)
    d = devices(client)
    assert len(d) == 1 and d[0]["device_id"] == "unit-7" and d[0]["streams"] == ["physiological", "tracking"] and d[0]["status_age_seconds"] == 1.5


def test_missing_device_ids_and_missing_statuses(client):
    register(client, "a1")
    register(client, "a2", jersey=2)
    phys(client, "a1", -1, "w-1")        # status never reported for w-1
    track(client, "a1", -1)              # tracking sample without a device id
    track(client, "a2", -1)              # a2: tracking only, no device id at all
    d1 = devices(client, "a1")
    assert len(d1) == 1 and d1[0]["device_id"] == "w-1" and d1[0]["streams"] == ["physiological"] and d1[0]["status"] is None and d1[0]["status_age_seconds"] is None
    assert devices(client, "a2") == []
    body = client.get("/api/v1/teams/team-a/live").json()
    assert "connected" not in json.dumps(body["athletes"][1]), "no connection state is invented for an athlete without a reported device"


def test_older_status_reports_do_not_replace_newer_ones(client):
    register(client, "a1")
    phys(client, "a1", -1, "w-1")
    status(client, "w-1", -5, True, 60, 0.8)
    status(client, "w-1", -50, False, 10, 0.1)   # older report arriving later: ignored by the store
    d = devices(client)[0]
    assert d["status"]["connected"] is True and d["status"]["battery_percent"] == 60 and d["status_age_seconds"] == 5.0


def test_zero_battery_and_zero_signal_are_real_values_and_nulls_stay_null(client):
    register(client, "a1")
    phys(client, "a1", -1, "w-1")
    track(client, "a1", -1, "c-1")
    status(client, "w-1", -1, True, 0, 0.0)
    status(client, "c-1", -1, True, None, None)
    d = devices(client)
    assert d[0]["status"]["battery_percent"] == 0 and d[0]["status"]["signal_quality"] == 0.0
    assert d[1]["status"]["battery_percent"] is None and d[1]["status"]["signal_quality"] is None


def test_stale_status_is_shown_with_its_age_not_dropped_and_not_turned_into_disconnected(client):
    register(client, "a1")
    phys(client, "a1", -1, "w-1")
    status(client, "w-1", -3600, True, 80, 0.7)
    d = devices(client)[0]
    assert d["status"]["connected"] is True and d["status_age_seconds"] == 3600.0


def test_only_device_ids_named_by_the_newest_samples_count(client):
    register(client, "a1")
    phys(client, "a1", -30, "old-strap")
    phys(client, "a1", -1, "new-strap")
    status(client, "old-strap", -1)
    d = devices(client)
    assert [x["device_id"] for x in d] == ["new-strap"] and d[0]["status"] is None, "a device named only by an older sample is not associated"


def test_legacy_tracking_rows_without_device_metadata_report_no_device(client, isolated_db):
    register(client, "old")
    legacy = {"athlete_id": "old", "timestamp": at(-1).isoformat(), **TRK}
    with isolated_db.transaction() as c:
        c.execute("INSERT INTO tracking_samples (athlete_id, ts_utc, payload) VALUES (?, ?, ?)", ("old", to_utc_iso(at(-1)), json.dumps(legacy)))
    row = client.get("/api/v1/teams/team-a/live").json()["athletes"][0]
    assert row["tracking"]["speed_mps"] == 3.0 and row["tracking"]["device_id"] is None and row["devices"] == []


def test_devices_are_per_athlete_and_a_status_after_the_captured_instant_has_age_zero(client):
    register(client, "a1")
    register(client, "b1", team_id="team-b")
    phys(client, "a1", -1, "w-a")
    phys(client, "b1", -1, "w-b")
    status(client, "w-a", 2, True, 55, 0.5)   # reported within the skew allowance, after the captured instant
    status(client, "w-b", -1)
    da = devices(client, "a1")
    assert [x["device_id"] for x in da] == ["w-a"] and da[0]["status_age_seconds"] == 0.0
    assert [x["device_id"] for x in devices(client, "b1", "team-b")] == ["w-b"]


def test_simulator_status_flag_is_opt_in_and_fictional():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "scripts" / "simulate_athlete.py").read_text(encoding="utf-8")
    assert '"--with-device-status", action="store_true"' in source and "fictional" in source.split("--with-device-status")[1][:400]
    assert "i % 5 == 0" in source
