"""Per-stream retention caps: PHYSIOLOGICAL_RETENTION_SAMPLES and TRACKING_RETENTION_SAMPLES.

Defaults stay 300 each. Each stream trims to its own cap by recorded timestamp, duplicates replace rather than
consume the cap, the retained-data and export responses report each stream's cap, a database is never pruned on
open (a reduced cap applies at the next accepted sample of that athlete's stream), invalid values are rejected, and
the history query limit is a separate API limit. All data is fictional.
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core import clock
from app.core.config import Settings, get_settings
from app.db import Database, set_database
from app.main import create_app
from app.models import RetainedData, TrackingSample
from app.store.history import DEFAULT_RETENTION_SAMPLES, MAX_HISTORY_SAMPLES, retention_cap, retention_caps
from app.store.samples import SampleStore
from app.store.tracking import TrackingStore

PHYS = "/api/v1/samples/physiological"
TRACK = "/api/v1/samples/tracking"
T0 = datetime(2026, 10, 20, 10, 0, 0, tzinfo=UTC)
NOW = datetime(2026, 10, 20, 10, 10, 0, tzinfo=UTC)
TRK = {"x_position": 1.0, "y_position": 2.0, "speed_mps": 3.0, "acceleration_mps2": 0.0, "tracking_confidence": 0.9}


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


@pytest.fixture
def caps(monkeypatch):
    """Configure the caps for one test through the environment, exactly as a .env or shell would; never left cached."""
    def configure(physiological=None, tracking=None):
        if physiological is not None:
            monkeypatch.setenv("PHYSIOLOGICAL_RETENTION_SAMPLES", str(physiological))
        if tracking is not None:
            monkeypatch.setenv("TRACKING_RETENTION_SAMPLES", str(tracking))
        get_settings.cache_clear()
    yield configure
    get_settings.cache_clear()


def post_phys(client, athlete_id, ts, hr=100):
    r = client.post(PHYS, json={"athlete_id": athlete_id, "timestamp": ts.isoformat(), "source": "simulated", "device_id": "sim", "heart_rate_bpm": hr})
    assert r.status_code == 200, r.text


def post_track(client, athlete_id, ts, speed=3.0):
    r = client.post(TRACK, json={"athlete_id": athlete_id, "timestamp": ts.isoformat(), **TRK, "speed_mps": speed})
    assert r.status_code == 200, r.text


def retained(client, athlete_id="ath-1"):
    r = client.get(f"/api/v1/athletes/{athlete_id}/retained-data")
    assert r.status_code == 200, r.text
    return r.json()


def stamps(client, athlete_id, stream):
    return [datetime.fromisoformat(s["timestamp"]) for s in client.get(f"/api/v1/athletes/{athlete_id}/{stream}/history?limit=300").json()]


# ---- defaults and validation -----------------------------------------------------------------------------------

def test_defaults_are_300_per_stream_and_the_old_name_is_the_default(monkeypatch):
    monkeypatch.delenv("PHYSIOLOGICAL_RETENTION_SAMPLES", raising=False)
    monkeypatch.delenv("TRACKING_RETENTION_SAMPLES", raising=False)
    s = Settings(_env_file=None)
    assert s.physiological_retention_samples == s.tracking_retention_samples == 300
    assert DEFAULT_RETENTION_SAMPLES == MAX_HISTORY_SAMPLES == 300
    assert retention_caps() == {"physiological": 300, "tracking": 300}


@pytest.mark.parametrize("bad", [0, -1, 1.5, "abc", "1.5", "", True, None])
def test_invalid_caps_are_rejected(bad):
    for field in ("physiological_retention_samples", "tracking_retention_samples"):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **{field: bad})


def test_invalid_environment_value_is_rejected_at_startup(monkeypatch):
    monkeypatch.setenv("TRACKING_RETENTION_SAMPLES", "0")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
    monkeypatch.setenv("TRACKING_RETENTION_SAMPLES", "3000")
    assert Settings(_env_file=None).tracking_retention_samples == 3000
    with pytest.raises(ValueError):
        Settings(_env_file=None).retention_cap("devices")


def test_store_rejects_a_non_positive_explicit_cap(isolated_db):
    for bad in (0, -5, 2.5, True):
        with pytest.raises(ValueError):
            SampleStore("tracking_samples", TrackingSample, bad, isolated_db)


# ---- independent caps, trimming, out-of-order arrival, duplicates ---------------------------------------------------

def test_each_stream_trims_to_its_own_cap_by_recorded_timestamp(client, caps):
    caps(physiological=3, tracking=5)
    assert retention_cap("physiological") == 3 and retention_cap("tracking") == 5
    for s in (5, 1, 9, 3, 7, 2, 8):          # out of order on purpose
        post_phys(client, "ath-1", at(s))
        post_track(client, "ath-1", at(s))
    assert stamps(client, "ath-1", "physiological") == [at(7), at(8), at(9)], "newest three by timestamp, not by arrival"
    assert stamps(client, "ath-1", "tracking") == [at(3), at(5), at(7), at(8), at(9)]
    body = retained(client)
    assert body["physiological"] == {"sample_count": 3, "max_samples": 3, "earliest_timestamp": "2026-10-20T10:00:07Z", "latest_timestamp": "2026-10-20T10:00:09Z"}
    assert body["tracking"] == {"sample_count": 5, "max_samples": 5, "earliest_timestamp": "2026-10-20T10:00:03Z", "latest_timestamp": "2026-10-20T10:00:09Z"}
    assert body["max_samples_per_stream"] is None, "no single shared cap exists when the two differ"
    RetainedData.model_validate(body)
    # an older sample arriving after the trim is inserted and trimmed away again in the same write: it never displaces a newer row
    post_phys(client, "ath-1", at(0))
    assert stamps(client, "ath-1", "physiological") == [at(7), at(8), at(9)]


def test_equal_instant_replacement_does_not_consume_the_cap(client, caps):
    caps(physiological=3, tracking=3)
    for s in (0, 1, 2):
        post_track(client, "ath-1", at(s), speed=float(s))
    post_track(client, "ath-1", at(2), speed=9.5)           # same instant: replaced, not added
    post_track(client, "ath-1", datetime.fromisoformat("2026-10-20T12:00:01+02:00"), speed=4.5)   # 10:00:01Z in another offset: replaced too
    assert stamps(client, "ath-1", "tracking") == [at(0), at(1), at(2)]
    assert [s["speed_mps"] for s in client.get("/api/v1/athletes/ath-1/tracking/history?limit=10").json()] == [0.0, 4.5, 9.5]
    assert client.get("/api/v1/athletes/ath-1/tracking/latest").json()["speed_mps"] == 9.5


def test_shared_cap_is_still_reported_when_both_streams_agree(client, caps):
    caps(physiological=7, tracking=7)
    body = retained(client, "nobody")
    assert body["physiological"]["max_samples"] == body["tracking"]["max_samples"] == body["max_samples_per_stream"] == 7


def test_athletes_and_streams_are_trimmed_independently(client, caps):
    caps(physiological=2, tracking=4)
    for s in range(6):
        post_phys(client, "ath-1", at(s)); post_track(client, "ath-1", at(s))
        post_phys(client, "ath-2", at(s + 100))
    assert retained(client, "ath-1")["physiological"]["sample_count"] == 2 and retained(client, "ath-1")["tracking"]["sample_count"] == 4
    assert retained(client, "ath-2")["physiological"]["sample_count"] == 2 and retained(client, "ath-2")["tracking"]["sample_count"] == 0


# ---- live reading and the plain latest endpoints are unchanged ---------------------------------------------------------

def test_live_snapshot_and_latest_are_unchanged_by_the_caps(client, caps, monkeypatch):
    caps(physiological=2, tracking=2)
    monkeypatch.setattr(clock, "utc_now", lambda: NOW)
    for s in (0, 1, 2, 3):
        post_phys(client, "ath-1", at(s), hr=100 + s); post_track(client, "ath-1", at(s), speed=float(s))
    live = client.get("/api/v1/athletes/ath-1/live").json()
    assert live["physiological"]["heart_rate_bpm"] == 103 and live["tracking"]["speed_mps"] == 3.0
    assert live["physiological_age_seconds"] == (NOW - at(3)).total_seconds()
    assert client.get("/api/v1/athletes/ath-1/physiological/latest").json()["heart_rate_bpm"] == 103
    assert client.get("/api/v1/athletes/ath-1/tracking/history?limit=10").json()[0]["speed_mps"] == 2.0, "only the newest two remain"


# ---- export, history query limit ---------------------------------------------------------------------------------------

def test_export_returns_every_retained_sample_under_a_cap_above_300_and_reports_both_caps(client, caps, isolated_db, monkeypatch):
    caps(physiological=2, tracking=320)
    monkeypatch.setattr(clock, "utc_now", lambda: NOW)
    store = TrackingStore(db=isolated_db)           # the configured cap applies: no explicit cap given
    assert store.max_samples == 320
    for s in range(330):
        store.add(TrackingSample(athlete_id="ath-1", timestamp=at(s), **TRK))
    for s in (0, 1, 2):
        post_phys(client, "ath-1", at(s))
    r = client.get("/api/v1/athletes/ath-1/retained-samples.jsonl")
    assert r.status_code == 200
    lines = r.text.splitlines()
    assert sum(1 for l in lines if '"type": "tracking"' in l) == 320 and sum(1 for l in lines if '"type": "physiological"' in l) == 2
    assert r.headers["X-Athletrix-Tracking-Count"] == "320" and r.headers["X-Athletrix-Physiological-Count"] == "2"
    assert r.headers["X-Athletrix-Tracking-Cap"] == "320" and r.headers["X-Athletrix-Physiological-Cap"] == "2"
    assert "X-Athletrix-Tracking-Cap" in r.headers["Access-Control-Expose-Headers"]
    # history is a page of at most 300 whatever the cap: a separate API limit, deliberately not widened
    assert client.get("/api/v1/athletes/ath-1/tracking/history?limit=320").status_code == 422
    assert len(client.get("/api/v1/athletes/ath-1/tracking/history?limit=300").json()) == 300
    assert client.get("/api/v1/athletes/ath-1/retained-data").json()["tracking"] == {"sample_count": 320, "max_samples": 320, "earliest_timestamp": "2026-10-20T10:00:10Z", "latest_timestamp": "2026-10-20T10:05:29Z"}
    # the window's 300 s limit is likewise untouched
    assert client.get("/api/v1/athletes/ath-1/window?seconds=301").status_code == 422


# ---- persistence: no pruning on open; a reduced cap applies at the next accepted sample ---------------------------------

def test_reopening_with_a_smaller_cap_prunes_nothing_until_the_next_write(tmp_path, caps, athlete_payload):
    path = tmp_path / "retention.db"
    caps(physiological=6, tracking=6)
    db = Database(path); set_database(db)
    with TestClient(create_app()) as c:
        for s in range(6):
            post_phys(c, "ath-1", at(s)); post_phys(c, "ath-2", at(s)); post_track(c, "ath-1", at(s))
        assert retained(c)["physiological"]["sample_count"] == 6
    db.close(); set_database(None)

    caps(physiological=3, tracking=6)                 # restart with a smaller physiological cap
    db = Database(path); set_database(db)
    try:
        with TestClient(create_app()) as c:
            body = retained(c)
            assert body["physiological"] == {"sample_count": 6, "max_samples": 3, "earliest_timestamp": "2026-10-20T10:00:00Z", "latest_timestamp": "2026-10-20T10:00:05Z"}, "opening the database prunes nothing: 6 rows under a cap of 3"
            assert stamps(c, "ath-1", "physiological") == [at(s) for s in range(6)]
            post_phys(c, "ath-1", at(6))              # the next accepted sample applies the cap to this athlete's stream only
            assert stamps(c, "ath-1", "physiological") == [at(4), at(5), at(6)]
            assert retained(c, "ath-2")["physiological"]["sample_count"] == 6, "another athlete is untouched until its own next write"
            assert retained(c)["tracking"]["sample_count"] == 6, "the other stream keeps its own cap"
            caps(physiological=10, tracking=6)                # raising the cap brings nothing back
            post_phys(c, "ath-1", at(7))
            assert stamps(c, "ath-1", "physiological") == [at(4), at(5), at(6), at(7)]
            assert retained(c)["physiological"]["max_samples"] == 10
    finally:
        db.close(); set_database(None)
