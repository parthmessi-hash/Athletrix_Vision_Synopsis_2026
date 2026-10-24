"""Latest-device-status store and its endpoints."""

from datetime import UTC, datetime, timedelta

import pytest

from app.core import clock
from app.models import DeviceStatus
from app.store.device import LatestDeviceStatusStore

T0 = "2026-10-15T12:00:00Z"
T1 = "2026-10-15T12:00:01Z"
POST = "/api/v1/devices/status"


def latest_url(device_id: str = "dev-1") -> str:
    return f"/api/v1/devices/{device_id}/status/latest"


def make_status(device_id="dev-1", timestamp=T0, battery=50.0, connected=True):
    return DeviceStatus(device_id=device_id, timestamp=timestamp, connected=connected, battery_percent=battery)


# ---- store unit tests ----------------------------------------------------

def test_store_empty_returns_none():
    assert LatestDeviceStatusStore().get("dev-1") is None


def test_store_stores_and_retrieves():
    store = LatestDeviceStatusStore()
    store.upsert(make_status(battery=50.0))
    assert store.get("dev-1").battery_percent == 50.0


def test_store_keeps_newer_status():
    store = LatestDeviceStatusStore()
    store.upsert(make_status(timestamp=T0, battery=50.0))
    store.upsert(make_status(timestamp=T1, battery=49.0))
    assert store.get("dev-1").battery_percent == 49.0


def test_store_ignores_older_status():
    store = LatestDeviceStatusStore()
    store.upsert(make_status(timestamp=T1, battery=49.0))
    returned = store.upsert(make_status(timestamp=T0, battery=50.0))
    assert store.get("dev-1").battery_percent == 49.0
    assert returned.battery_percent == 49.0


def test_store_equal_timestamp_takes_most_recent_write():
    store = LatestDeviceStatusStore()
    store.upsert(make_status(timestamp=T0, connected=True))
    store.upsert(make_status(timestamp=T0, connected=False))
    assert store.get("dev-1").connected is False


def test_store_is_per_device():
    store = LatestDeviceStatusStore()
    store.upsert(make_status("dev-1", battery=50.0))
    store.upsert(make_status("dev-2", battery=10.0))
    assert store.get("dev-1").battery_percent == 50.0
    assert store.get("dev-2").battery_percent == 10.0


# ---- endpoint tests ------------------------------------------------------

def test_get_latest_404_when_unknown(client):
    r = client.get(latest_url("nobody"))
    assert r.status_code == 404
    assert "nobody" in r.json()["detail"]


def test_post_then_get_latest(client, device_status_payload):
    assert client.post(POST, json=device_status_payload).status_code == 200
    r = client.get(latest_url())
    assert r.status_code == 200
    assert r.json()["connected"] is True
    assert r.json()["battery_percent"] == device_status_payload["battery_percent"]


def test_get_latest_returns_newest_not_last_posted(client, device_status_payload):
    newer = {**device_status_payload, "timestamp": T1, "battery_percent": 40}
    older = {**device_status_payload, "timestamp": T0, "battery_percent": 90}
    assert client.post(POST, json=newer).status_code == 200
    assert client.post(POST, json=older).status_code == 200  # accepted, just not stored as latest
    assert client.get(latest_url()).json()["battery_percent"] == 40


def test_devices_are_independent(client, device_status_payload):
    client.post(POST, json={**device_status_payload, "device_id": "dev-1", "battery_percent": 80})
    client.post(POST, json={**device_status_payload, "device_id": "dev-2", "battery_percent": 5})
    assert client.get(latest_url("dev-1")).json()["battery_percent"] == 80
    assert client.get(latest_url("dev-2")).json()["battery_percent"] == 5


def test_missing_optionals_stay_null(client):
    r = client.post(POST, json={"device_id": "dev-1", "timestamp": T0, "connected": False})
    assert r.status_code == 200
    body = client.get(latest_url()).json()
    assert body["battery_percent"] is None
    assert body["signal_quality"] is None
    assert body["connected"] is False


def test_zero_values_are_preserved(client):
    r = client.post(POST, json={"device_id": "dev-1", "timestamp": T0, "connected": True,
                                "battery_percent": 0, "signal_quality": 0.0})
    assert r.status_code == 200
    body = client.get(latest_url()).json()
    assert body["battery_percent"] == 0
    assert body["signal_quality"] == 0.0


def test_invalid_post_does_not_touch_store(client, device_status_payload):
    r = client.post(POST, json={**device_status_payload, "battery_percent": 150})
    assert r.status_code == 422
    assert client.get(latest_url()).status_code == 404


# ---- future-timestamp rule applies here too ------------------------------

@pytest.fixture
def frozen_clock(monkeypatch):
    now = datetime(2026, 10, 24, 12, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(clock, "utc_now", lambda: now)
    return now


def test_future_status_rejected_and_store_unchanged(client, device_status_payload, frozen_clock):
    good = {**device_status_payload, "timestamp": (frozen_clock - timedelta(seconds=1)).isoformat(), "battery_percent": 70}
    future = {**device_status_payload, "timestamp": (frozen_clock + timedelta(minutes=10)).isoformat(), "battery_percent": 1}
    assert client.post(POST, json=good).status_code == 200
    r = client.post(POST, json=future)
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["body", "timestamp"]
    assert client.get(latest_url()).json()["battery_percent"] == 70


def test_future_status_rejected_leaves_empty_store_empty(client, device_status_payload, frozen_clock):
    future = {**device_status_payload, "timestamp": (frozen_clock + timedelta(minutes=10)).isoformat()}
    assert client.post(POST, json=future).status_code == 422
    assert client.get(latest_url()).status_code == 404
