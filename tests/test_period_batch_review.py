"""Batch review of one athlete's saved periods and its CSV, checked against hand-calculated values. All data is fictional.

Scene (athlete a1, 1 Hz, confidence 1.0): speed 0, 2, 4 m/s at 0-2 s, then 6 m/s at 3-10 s; heart rate 100 + s bpm, null at 2 s.
By hand, with the default rules (gap 3 s, confidence 0.5, threshold 5 m/s; before 10 s, after 30 s capped at backend time 60 s):
  period "=SUM(A1)" [1, 3] s: distance (2+4)/2 + (4+6)/2 = 8 m; above 5 m/s only after the crossing at 0.5 s into 2->3 s:
      0.5 s and (5+6)/2*0.5 = 2.75 m; before [-9, 1): 0 s only, mean 100; during [1, 3]: 1, 2, 3 s, two measured, mean 102;
      after (3, 33]: 4..10 s, mean 107
  period "steady" [5, 6] s: distance 6 m, 1 s and 6 m above the threshold
  period "steady" [20, 25] s: no movement samples: distance and high-speed null (not zero); during and after empty with null
      means; before [10, 20) holds only the 10 s sample (110 bpm)
With HIGH_SPEED_THRESHOLD_MPS=3 the first period has 0.5 s (crossing halfway 2->4) + 1 s = 1.5 s above it.
"""

import csv
import io
from datetime import UTC, datetime, timedelta

import pytest

from app.core import clock
from app.core.config import get_settings
from app.models.recording import PeriodBatchReview

NOW = datetime(2026, 10, 26, 20, 0, 0, tzinfo=UTC)
REC = "/api/v1/recordings"


def at(s):
    return NOW + timedelta(seconds=s)


def z(s):
    return at(s).isoformat().replace("+00:00", "Z")


@pytest.fixture
def frozen(monkeypatch):
    state = {"now": NOW}
    monkeypatch.setattr(clock, "utc_now", lambda: state["now"])
    return state


@pytest.fixture
def settings_env(monkeypatch):
    def configure(**kw):
        for k, v in kw.items():
            monkeypatch.setenv(k, str(v))
        get_settings.cache_clear()
    yield configure
    get_settings.cache_clear()


@pytest.fixture
def scene(client, frozen):
    for n, a in enumerate(("a1", "a2"), start=1):
        assert client.post("/api/v1/athletes", json={"id": a, "name": f"Fictional {a}", "jersey_number": n, "sport": "fictional", "team_id": "t"}).status_code == 201
    rec = client.post(REC, json={"name": "hand check", "athlete_ids": ["a1", "a2"]}).json()["id"]
    for s in range(11):
        frozen["now"] = at(s)
        assert client.post("/api/v1/samples/physiological", json={"athlete_id": "a1", "timestamp": z(s), "source": "wearable", "device_id": "w", "heart_rate_bpm": None if s == 2 else 100 + s}).status_code == 200
        assert client.post("/api/v1/samples/tracking", json={"athlete_id": "a1", "timestamp": z(s), "x_position": 0, "y_position": 0, "speed_mps": [0.0, 2.0, 4.0][s] if s < 3 else 6.0, "acceleration_mps2": 0, "tracking_confidence": 1.0}).status_code == 200
    frozen["now"] = at(30)
    client.post(f"{REC}/{rec}/stop")
    for label, a, b, who in (("steady", 20, 25, "a1"), ("=SUM(A1)", 1, 3, "a1"), ("steady", 5, 6, "a1"), ("other athlete", 1, 3, "a2")):
        assert client.post(f"{REC}/{rec}/athletes/{who}/periods", json={"label": label, "effort_start": z(a), "effort_end": z(b)}).status_code == 201
    frozen["now"] = at(60)
    return rec


def test_batch_review_matches_hand_calculation_in_start_order_without_ranking(client, scene):
    body = client.get(f"{REC}/{scene}/athletes/a1/period-reviews").json()
    PeriodBatchReview.model_validate(body)
    assert body["captured_at"] == z(60) and body["athlete"]["id"] == "a1" and body["recording"]["id"] == scene
    assert body["distance_rules"] == {"max_gap_seconds": 3.0, "min_confidence": 0.5, "high_speed_threshold_mps": 5.0} and body["rules"]["after_seconds"] == 30.0
    items = body["items"]
    assert [i["period"]["label"] for i in items] == ["=SUM(A1)", "steady", "steady"], "listed by start instant; labels may repeat"
    assert all(i["review"]["captured_at"] == z(60) for i in items), "one captured time for the batch"
    first, second, empty = (i["review"] for i in items)
    m = first["movement"]
    assert m["sample_count"] == 3 and m["estimated_distance_m"] == pytest.approx(8.0) and m["high_speed_duration_seconds"] == pytest.approx(0.5) and m["high_speed_distance_m"] == pytest.approx(2.75)
    hr = first["heart_rate"]
    assert (hr["before"]["sample_count"], hr["before"]["heart_rate_mean_bpm"]) == (1, 100.0)
    assert (hr["during"]["sample_count"], hr["during"]["heart_rate_count"], hr["during"]["heart_rate_mean_bpm"]) == (3, 2, 102.0)
    assert (hr["after"]["sample_count"], hr["after"]["heart_rate_mean_bpm"], hr["after"]["period_end"]) == (7, 107.0, z(33))
    assert second["movement"]["estimated_distance_m"] == pytest.approx(6.0) and second["movement"]["high_speed_duration_seconds"] == pytest.approx(1.0)
    assert empty["movement"]["sample_count"] == 0 and empty["movement"]["estimated_distance_m"] is None and empty["movement"]["high_speed_duration_seconds"] is None
    assert (empty["heart_rate"]["before"]["sample_count"], empty["heart_rate"]["before"]["heart_rate_mean_bpm"]) == (1, 110.0), "before [10, 20) holds the 10 s sample"
    assert all(empty["heart_rate"][n]["sample_count"] == 0 and empty["heart_rate"][n]["heart_rate_mean_bpm"] is None for n in ("during", "after"))
    # identical to the single effort review and the detailed JSON export at the same instant
    single = client.get(f"{REC}/{scene}/athletes/a1/effort-review", params={"effort_start": z(1), "effort_end": z(3)}).json()
    assert single == first
    detail = client.get(f"{REC}/{scene}/athletes/a1/periods/{items[0]['period']['id']}/review.json").json()
    assert detail["review"] == first
    assert [i["period"]["athlete_id"] for i in client.get(f"{REC}/{scene}/athletes/a2/period-reviews").json()["items"]] == ["a2"], "never mixed across athletes"
    def keys(o):
        if isinstance(o, dict):
            for k, v in o.items():
                yield k.lower(); yield from keys(v)
        elif isinstance(o, list):
            for v in o:
                yield from keys(v)
    assert not any(w in k for k in keys(body) for w in ("rank", "fatigue", "readiness", "recover", "score")), "no ranking or interpretive field"


def test_csv_has_documented_columns_empty_nulls_real_zeros_and_safe_text(client, scene):
    r = client.get(f"{REC}/{scene}/athletes/a1/period-reviews.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv") and r.headers["X-Athletrix-Period-Count"] == "3"
    assert r.headers["Content-Disposition"] == f'attachment; filename="athletrix-period-reviews-{scene}-a1-20261026T200100Z.csv"'
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert len(rows) == 3
    from app.api.recordings import CSV_COLUMNS
    assert list(rows[0]) == [c for c, _ in CSV_COLUMNS]
    first, _, empty = rows
    assert first["label"] == "'=SUM(A1)", "a formula-looking label is neutralised for spreadsheets"
    assert (first["distance_m"], first["high_speed_duration_s"], first["high_speed_distance_m"]) == ("8.0", "0.5", "2.75")
    assert (first["hr_during_sample_count"], first["hr_during_measured_count"], first["hr_during_mean_bpm"]) == ("3", "2", "102.0")
    assert (first["effort_start"], first["effort_end"], first["duration_s"], first["calculated_at"]) == (z(1), z(3), "2.0", z(60))
    assert (first["high_speed_threshold_mps"], first["max_gap_s"], first["min_confidence"]) == ("5.0", "3.0", "0.5")
    assert empty["movement_sample_count"] == "0" and empty["distance_m"] == "" and empty["included_duration_s"] == "0.0", "null is an empty cell; a real zero is written"
    assert empty["hr_during_mean_bpm"] == "" and empty["hr_during_sample_count"] == "0"


def test_a_changed_setting_changes_the_batch_and_csv_but_not_the_saved_bounds(client, scene, settings_env):
    before = client.get(f"{REC}/{scene}/athletes/a1/period-reviews").json()
    settings_env(HIGH_SPEED_THRESHOLD_MPS="3.0")
    after = client.get(f"{REC}/{scene}/athletes/a1/period-reviews").json()
    assert after["distance_rules"]["high_speed_threshold_mps"] == 3.0
    assert after["items"][0]["review"]["movement"]["high_speed_duration_seconds"] == pytest.approx(1.5)
    assert [i["period"] for i in after["items"]] == [i["period"] for i in before["items"]]
    row = next(csv.DictReader(io.StringIO(client.get(f"{REC}/{scene}/athletes/a1/period-reviews.csv").text)))
    assert row["high_speed_threshold_mps"] == "3.0" and row["high_speed_duration_s"] == "1.5"


def test_active_unknown_nonmember_and_no_periods(client, scene, frozen):
    assert client.get(f"{REC}/rec-nope/athletes/a1/period-reviews").status_code == 404
    assert client.get(f"{REC}/{scene}/athletes/a9/period-reviews").status_code == 404
    client.post("/api/v1/athletes", json={"id": "a3", "name": "Fictional a3", "jersey_number": 3, "sport": "fictional", "team_id": "t"})
    live = client.post(REC, json={"name": "live", "athlete_ids": ["a3"]}).json()["id"]
    assert client.get(f"{REC}/{live}/athletes/a3/period-reviews").status_code == 409
    frozen["now"] = at(70)
    client.post(f"{REC}/{live}/stop")
    empty = client.get(f"{REC}/{live}/athletes/a3/period-reviews").json()
    assert empty["items"] == [] and empty["rules"]["before_seconds"] == 10.0
    assert client.get(f"{REC}/{live}/athletes/a3/period-reviews.csv").text.count("\r\n") == 1, "only the header row"


def test_inventory_counts_per_recording(client, scene):
    inv = client.get(f"{REC}/inventory").json()
    assert len(inv) == 1 and inv[0]["id"] == scene and inv[0]["status"] == "stopped"
    assert (inv[0]["roster_size"], inv[0]["physiological_samples"], inv[0]["tracking_samples"], inv[0]["saved_periods"], inv[0]["imported"]) == (2, 11, 11, 4, False)
    assert inv[0]["athletes"][0]["id"] == "a1", "the plain recording fields are kept, so list consumers keep working"
