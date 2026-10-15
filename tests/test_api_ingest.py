"""HTTP-level tests: endpoints validate and echo payloads, rejecting bad input with 422."""


def test_validate_athlete_ok(client, athlete_payload):
    r = client.post("/api/v1/athletes/validate", json=athlete_payload)
    assert r.status_code == 200
    assert r.json() == athlete_payload


def test_validate_athlete_rejects_bad_jersey(client, athlete_payload):
    r = client.post("/api/v1/athletes/validate", json={**athlete_payload, "jersey_number": -5})
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"][-1] == "jersey_number"


def test_physiological_ok(client, physiological_payload):
    r = client.post("/api/v1/samples/physiological", json=physiological_payload)
    assert r.status_code == 200
    body = r.json()
    assert body["heart_rate_bpm"] == 142
    assert body["posture"] == "running"
    assert body["timestamp"].startswith("2026-10-15T12:00:00")


def test_physiological_optional_fields_default_to_null(client):
    r = client.post(
        "/api/v1/samples/physiological",
        json={"athlete_id": "ath-1", "timestamp": "2026-10-15T12:00:00Z", "source": "manual", "device_id": "coach"},
    )
    assert r.status_code == 200
    assert r.json()["heart_rate_bpm"] is None


def test_physiological_rejects_out_of_range(client, physiological_payload):
    r = client.post("/api/v1/samples/physiological", json={**physiological_payload, "spo2_percent": 140})
    assert r.status_code == 422


def test_physiological_rejects_naive_timestamp(client, physiological_payload):
    r = client.post("/api/v1/samples/physiological", json={**physiological_payload, "timestamp": "2026-10-15T12:00:00"})
    assert r.status_code == 422


def test_physiological_rejects_unknown_field(client, physiological_payload):
    r = client.post("/api/v1/samples/physiological", json={**physiological_payload, "vendor_raw": {}})
    assert r.status_code == 422


def test_tracking_ok(client, tracking_payload):
    r = client.post("/api/v1/samples/tracking", json=tracking_payload)
    assert r.status_code == 200
    assert r.json()["speed_mps"] == 6.4


def test_tracking_rejects_negative_speed(client, tracking_payload):
    r = client.post("/api/v1/samples/tracking", json={**tracking_payload, "speed_mps": -2})
    assert r.status_code == 422


def test_tracking_rejects_missing_field(client, tracking_payload):
    del tracking_payload["tracking_confidence"]
    r = client.post("/api/v1/samples/tracking", json=tracking_payload)
    assert r.status_code == 422


def test_device_status_ok(client, device_status_payload):
    r = client.post("/api/v1/devices/status", json=device_status_payload)
    assert r.status_code == 200
    assert r.json()["connected"] is True


def test_device_status_rejects_bad_battery(client, device_status_payload):
    r = client.post("/api/v1/devices/status", json={**device_status_payload, "battery_percent": 150})
    assert r.status_code == 422
