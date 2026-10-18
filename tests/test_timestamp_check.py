"""Future-timestamp rejection on the sample POST endpoints, using a controlled clock."""

from datetime import UTC, datetime, timedelta

import pytest

from app.core import clock

NOW = datetime(2026, 10, 18, 12, 0, 0, tzinfo=UTC)
TOLERANCE = clock.MAX_FUTURE_TIMESTAMP_SKEW


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    """Pin the backend clock so timestamp offsets are deterministic."""
    monkeypatch.setattr(clock, "utc_now", lambda: NOW)


def ts(offset: timedelta) -> str:
    return (NOW + offset).isoformat()


def parse(iso: str) -> datetime:
    """Compare timestamps as datetimes; the API serialises UTC as 'Z', inputs use '+00:00'."""
    return datetime.fromisoformat(iso)


ENDPOINTS = [
    pytest.param("/api/v1/samples/physiological", "physiological_payload", "physiological", id="physiological"),
    pytest.param("/api/v1/samples/tracking", "tracking_payload", "tracking", id="tracking"),
]


def latest_url(kind: str) -> str:
    return f"/api/v1/athletes/ath-1/{kind}/latest"


# ---- accepted ------------------------------------------------------------

@pytest.mark.parametrize(
    "offset",
    [
        pytest.param(timedelta(hours=-1), id="past"),
        pytest.param(timedelta(0), id="exactly-now"),
        pytest.param(TOLERANCE - timedelta(milliseconds=1), id="just-inside-tolerance"),
        pytest.param(TOLERANCE, id="exactly-at-tolerance"),
    ],
)
@pytest.mark.parametrize("endpoint,payload_fixture,kind", ENDPOINTS)
def test_accepts_timestamps_within_tolerance(client, request, endpoint, payload_fixture, kind, offset):
    payload = {**request.getfixturevalue(payload_fixture), "timestamp": ts(offset)}
    r = client.post(endpoint, json=payload)
    assert r.status_code == 200
    assert client.get(latest_url(kind)).status_code == 200


# ---- rejected ------------------------------------------------------------

@pytest.mark.parametrize(
    "offset",
    [
        pytest.param(TOLERANCE + timedelta(milliseconds=1), id="just-over-tolerance"),
        pytest.param(timedelta(minutes=10), id="ten-minutes-ahead"),
        pytest.param(timedelta(days=1), id="one-day-ahead"),
    ],
)
@pytest.mark.parametrize("endpoint,payload_fixture,kind", ENDPOINTS)
def test_rejects_future_timestamps(client, request, endpoint, payload_fixture, kind, offset):
    payload = {**request.getfixturevalue(payload_fixture), "timestamp": ts(offset)}
    r = client.post(endpoint, json=payload)
    assert r.status_code == 422
    err = r.json()["detail"][0]
    assert err["loc"] == ["body", "timestamp"]
    assert "ahead of backend time" in err["msg"]
    assert f"{TOLERANCE.total_seconds():g}s" in err["msg"]


@pytest.mark.parametrize("endpoint,payload_fixture,kind", ENDPOINTS)
def test_rejected_sample_leaves_empty_store_empty(client, request, endpoint, payload_fixture, kind):
    payload = {**request.getfixturevalue(payload_fixture), "timestamp": ts(timedelta(minutes=10))}
    assert client.post(endpoint, json=payload).status_code == 422
    assert client.get(latest_url(kind)).status_code == 404


@pytest.mark.parametrize("endpoint,payload_fixture,kind", ENDPOINTS)
def test_rejected_sample_does_not_replace_stored_sample(client, request, endpoint, payload_fixture, kind):
    base = request.getfixturevalue(payload_fixture)
    good = {**base, "timestamp": ts(timedelta(seconds=-1))}
    future = {**base, "timestamp": ts(timedelta(minutes=10))}
    assert client.post(endpoint, json=good).status_code == 200
    assert client.post(endpoint, json=future).status_code == 422
    stored = client.get(latest_url(kind)).json()
    assert parse(stored["timestamp"]) == parse(good["timestamp"])


# ---- existing ordering rules still apply to accepted samples ------------

@pytest.mark.parametrize("endpoint,payload_fixture,kind", ENDPOINTS)
def test_older_sample_still_accepted_but_not_stored_as_latest(client, request, endpoint, payload_fixture, kind):
    base = request.getfixturevalue(payload_fixture)
    newer = {**base, "timestamp": ts(timedelta(seconds=-1))}
    older = {**base, "timestamp": ts(timedelta(seconds=-2))}
    assert client.post(endpoint, json=newer).status_code == 200
    assert client.post(endpoint, json=older).status_code == 200
    assert parse(client.get(latest_url(kind)).json()["timestamp"]) == parse(newer["timestamp"])


def test_equal_timestamp_still_takes_most_recent_write(client, physiological_payload):
    first = {**physiological_payload, "timestamp": ts(timedelta(0)), "heart_rate_bpm": 100}
    second = {**physiological_payload, "timestamp": ts(timedelta(0)), "heart_rate_bpm": 105}
    assert client.post("/api/v1/samples/physiological", json=first).status_code == 200
    assert client.post("/api/v1/samples/physiological", json=second).status_code == 200
    assert client.get(latest_url("physiological")).json()["heart_rate_bpm"] == 105
