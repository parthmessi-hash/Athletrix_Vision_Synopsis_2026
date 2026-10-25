"""GET /athletes/{id}/retained-data: counts and UTC recorded time bounds of the rows currently retained per stream.

The endpoint reads persisted rows only. It describes what is retained now (after equal-instant replacement and the
newest-300 trim), never coverage, sessions or evicted records.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.models import RetainedData, TrackingSample
from app.store.history import MAX_HISTORY_SAMPLES
from app.store.tracking import TrackingStore

PHYS = "/api/v1/samples/physiological"
TRACK = "/api/v1/samples/tracking"
T0 = datetime(2026, 10, 20, 10, 0, 0, tzinfo=UTC)  # a fictional recording well in the past


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def retained(client, athlete_id="ath-1"):
    r = client.get(f"/api/v1/athletes/{athlete_id}/retained-data")
    assert r.status_code == 200, r.text
    return r.json()


def post(client, path, base, ts, athlete_id="ath-1", **extra):
    ts_text = ts if isinstance(ts, str) else ts.isoformat()
    r = client.post(path, json={**base, "athlete_id": athlete_id, "timestamp": ts_text, **extra})
    assert r.status_code == 200, r.text


EMPTY = {"sample_count": 0, "earliest_timestamp": None, "latest_timestamp": None}


# ---- empty and single-stream -------------------------------------------------------------------------------

def test_unknown_or_empty_athlete_reports_zero_counts_null_timestamps_and_the_cap(client):
    body = retained(client, "never-seen")
    assert body == {"athlete_id": "never-seen", "physiological": EMPTY, "tracking": EMPTY, "max_samples_per_stream": MAX_HISTORY_SAMPLES}
    RetainedData.model_validate(body)  # the response is the documented model, nothing extra


def test_registered_athlete_without_samples_is_also_empty(client, athlete_payload):
    assert client.post("/api/v1/athletes", json=athlete_payload).status_code == 201
    body = retained(client, athlete_payload["id"])
    assert body["physiological"] == EMPTY and body["tracking"] == EMPTY


def test_single_physiological_stream(client, physiological_payload):
    post(client, PHYS, physiological_payload, at(0))
    post(client, PHYS, physiological_payload, at(4))
    post(client, PHYS, physiological_payload, at(2))
    body = retained(client)
    assert body["physiological"] == {"sample_count": 3, "earliest_timestamp": "2026-10-20T10:00:00Z", "latest_timestamp": "2026-10-20T10:00:04Z"}
    assert body["tracking"] == EMPTY, "the other stream stays empty; nothing is inferred across streams"


def test_single_tracking_stream_and_one_sample_has_equal_bounds(client, tracking_payload):
    post(client, TRACK, tracking_payload, at(7))
    body = retained(client)
    assert body["tracking"] == {"sample_count": 1, "earliest_timestamp": "2026-10-20T10:00:07Z", "latest_timestamp": "2026-10-20T10:00:07Z"}
    assert body["physiological"] == EMPTY


def test_streams_can_cover_different_times(client, physiological_payload, tracking_payload):
    post(client, PHYS, physiological_payload, at(0))
    post(client, PHYS, physiological_payload, at(10))
    post(client, TRACK, tracking_payload, at(100))
    post(client, TRACK, tracking_payload, at(130))
    body = retained(client)
    assert (body["physiological"]["earliest_timestamp"], body["physiological"]["latest_timestamp"]) == ("2026-10-20T10:00:00Z", "2026-10-20T10:00:10Z")
    assert (body["tracking"]["earliest_timestamp"], body["tracking"]["latest_timestamp"]) == ("2026-10-20T10:01:40Z", "2026-10-20T10:02:10Z")


# ---- athletes are separate ------------------------------------------------------------------------------------

def test_different_athlete_ids_are_counted_separately(client, physiological_payload, tracking_payload):
    post(client, PHYS, physiological_payload, at(0), athlete_id="ath-1")
    post(client, PHYS, physiological_payload, at(1), athlete_id="ath-1")
    post(client, TRACK, tracking_payload, at(50), athlete_id="ath-2")
    a, b, c = retained(client, "ath-1"), retained(client, "ath-2"), retained(client, "ath-3")
    assert a["athlete_id"] == "ath-1" and a["physiological"]["sample_count"] == 2 and a["tracking"] == EMPTY
    assert b["athlete_id"] == "ath-2" and b["physiological"] == EMPTY and b["tracking"]["sample_count"] == 1
    assert b["tracking"]["latest_timestamp"] == "2026-10-20T10:00:50Z"
    assert c["physiological"] == EMPTY and c["tracking"] == EMPTY


# ---- arrival order, offsets, duplicates -----------------------------------------------------------------------------

def test_out_of_order_arrival_bounds_follow_recorded_timestamps_not_arrival(client, tracking_payload):
    post(client, TRACK, tracking_payload, at(5))
    post(client, TRACK, tracking_payload, at(1))
    post(client, TRACK, tracking_payload, at(3))
    t = retained(client)["tracking"]
    assert (t["sample_count"], t["earliest_timestamp"], t["latest_timestamp"]) == (3, "2026-10-20T10:00:01Z", "2026-10-20T10:00:05Z")


def test_equivalent_offsets_are_reported_in_utc(client, physiological_payload):
    post(client, PHYS, physiological_payload, "2026-10-20T12:00:02+02:00")   # 10:00:02Z
    post(client, PHYS, physiological_payload, "2026-10-20T05:00:00-05:00")   # 10:00:00Z
    post(client, PHYS, physiological_payload, "2026-10-20T10:00:01.250Z")
    p = retained(client)["physiological"]
    assert p["sample_count"] == 3
    assert p["earliest_timestamp"] == "2026-10-20T10:00:00Z" and p["latest_timestamp"] == "2026-10-20T10:00:02Z"
    # the stored samples keep their offsets; only the bounds are normalised
    hist = client.get("/api/v1/athletes/ath-1/physiological/history?limit=10").json()
    assert [s["timestamp"] for s in hist] == ["2026-10-20T05:00:00-05:00", "2026-10-20T10:00:01.250000Z", "2026-10-20T12:00:02+02:00"]


def test_equal_instant_replacement_does_not_inflate_the_count(client, physiological_payload):
    post(client, PHYS, physiological_payload, at(0), heart_rate_bpm=100)
    post(client, PHYS, physiological_payload, "2026-10-20T12:00:00+02:00", heart_rate_bpm=101)   # same instant, other offset
    post(client, PHYS, physiological_payload, at(0), heart_rate_bpm=102)
    p = retained(client)["physiological"]
    assert p == {"sample_count": 1, "earliest_timestamp": "2026-10-20T10:00:00Z", "latest_timestamp": "2026-10-20T10:00:00Z"}
    assert client.get("/api/v1/athletes/ath-1/physiological/latest").json()["heart_rate_bpm"] == 102


def test_bounds_agree_with_history_and_latest_endpoints(client, tracking_payload):
    for s in (9, 2, 6, 4):
        post(client, TRACK, tracking_payload, at(s))
    t = retained(client)["tracking"]
    hist = client.get("/api/v1/athletes/ath-1/tracking/history?limit=300").json()
    latest = client.get("/api/v1/athletes/ath-1/tracking/latest").json()
    assert t["sample_count"] == len(hist) == 4
    assert datetime.fromisoformat(t["earliest_timestamp"]) == datetime.fromisoformat(hist[0]["timestamp"])
    assert datetime.fromisoformat(t["latest_timestamp"]) == datetime.fromisoformat(latest["timestamp"])


# ---- retention trimming ----------------------------------------------------------------------------------------

def test_store_retention_after_trimming_with_a_small_cap(isolated_db, tracking_payload):
    store = TrackingStore(max_samples=3, db=isolated_db)
    for s in (0, 1, 2, 3, 4):
        store.add(TrackingSample.model_validate({**tracking_payload, "athlete_id": "ath-1", "timestamp": at(s).isoformat()}))
    r = store.retention("ath-1")
    assert (r.count, r.earliest, r.latest) == (3, at(2), at(4)), "the two oldest were trimmed; the bounds describe what remains"
    assert store.retention("ath-9") == (0, None, None)


def test_api_counts_and_range_after_the_300_sample_trim(client, physiological_payload):
    extra = 5
    for s in range(MAX_HISTORY_SAMPLES + extra):
        post(client, PHYS, physiological_payload, at(s))
    body = retained(client)
    p = body["physiological"]
    assert p["sample_count"] == MAX_HISTORY_SAMPLES == body["max_samples_per_stream"]
    assert datetime.fromisoformat(p["earliest_timestamp"]) == at(extra), "the five oldest rows are gone and the range starts at the oldest retained row"
    assert datetime.fromisoformat(p["latest_timestamp"]) == at(MAX_HISTORY_SAMPLES + extra - 1)


def test_model_rejects_negative_counts_and_unknown_fields():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        RetainedData.model_validate({"athlete_id": "a", "physiological": {"sample_count": -1}, "tracking": EMPTY, "max_samples_per_stream": 300})
    with pytest.raises(ValidationError):
        RetainedData.model_validate({"athlete_id": "a", "physiological": EMPTY, "tracking": EMPTY, "max_samples_per_stream": 300, "sessions": []})
