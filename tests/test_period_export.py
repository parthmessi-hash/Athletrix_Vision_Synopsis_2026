"""GET .../periods/{pid}/review.json: a descriptive snapshot of a saved period's review, from one read and one set of settings.

The document carries the saved selection, the rules used now, the result and the normalised samples it came from;
it agrees with the session effort review, reflects a changed setting in a new export while the saved bounds stay the
same, and is distinct from the replay-compatible JSONL export. All data is fictional.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.core import clock
from app.core.config import get_settings
from app.models import RecordingEffortReview, SavedPeriod

NOW = datetime(2026, 10, 26, 20, 0, 0, tzinfo=UTC)
REC = "/api/v1/recordings"
PHYS = "/api/v1/samples/physiological"
TRACK = "/api/v1/samples/tracking"


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


def at(seconds):
    return NOW + timedelta(seconds=seconds)


def iso(seconds):
    return at(seconds).isoformat().replace("+00:00", "Z")


@pytest.fixture
def scene(client, frozen):
    """A stopped recording for a1 with a 20 s effort: some null heart rates, a low-confidence point, a real zero speed."""
    assert client.post("/api/v1/athletes", json={"id": "a1", "name": "Fictional One", "jersey_number": 1, "sport": "fictional", "team_id": "t"}).status_code == 201
    rec = client.post(REC, json={"name": "practice", "athlete_ids": ["a1"]}).json()["id"]
    for s in range(0, 40):
        frozen["now"] = at(s)
        hr = None if s % 7 == 0 else 100 + s
        assert client.post(PHYS, json={"athlete_id": "a1", "timestamp": at(s).isoformat(), "source": "wearable", "device_id": "band-9", "heart_rate_bpm": hr}).status_code == 200
        body = {"athlete_id": "a1", "timestamp": at(s).isoformat(), "source": "camera", "device_id": "cam-2", "x_position": float(s), "y_position": 0.0, "speed_mps": 0.0 if s == 12 else 3.0 + 0.2 * s, "acceleration_mps2": 0.2,
                "tracking_confidence": 0.2 if s == 15 else 0.9}
        assert client.post(TRACK, json=body).status_code == 200
    frozen["now"] = at(60)
    assert client.post(f"{REC}/{rec}/stop").status_code == 200
    period = client.post(f"{REC}/{rec}/athletes/a1/periods", json={"label": "main effort", "effort_start": iso(10), "effort_end": iso(30)}).json()
    frozen["now"] = at(120)
    return {"rec": rec, "period": period}


def export(client, scene, period_id=None):
    r = client.get(f"{REC}/{scene['rec']}/athletes/a1/periods/{period_id or scene['period']['id']}/review.json")
    assert r.status_code == 200, r.text
    return r, json.loads(r.text)


def test_export_document_agrees_with_the_session_review_and_carries_its_samples(client, scene):
    r, doc = export(client, scene)
    assert r.headers["content-type"].startswith("application/json") and r.headers["X-Athletrix-Format"] == "athletrix-period-review/1"
    assert r.headers["Content-Disposition"] == f'attachment; filename="athletrix-period-review-{scene["rec"]}-a1-{scene["period"]["id"]}-20261026T200200Z.json"'
    assert doc["format"] == "athletrix-period-review" and doc["format_version"] == 1 and doc["generated_at"] == iso(120)
    assert doc["recording"]["id"] == scene["rec"] and doc["recording"]["status"] == "stopped" and doc["recording"]["athletes"][0]["id"] == "a1"
    assert doc["saved_period"] == scene["period"] and doc["athlete"]["name"] == "Fictional One"
    SavedPeriod.model_validate(doc["saved_period"])
    assert doc["calculation"]["distance_rules"] == {"max_gap_seconds": 3.0, "min_confidence": 0.5, "high_speed_threshold_mps": 5.0} and doc["calculation"]["tracking_rules"] == {"min_confidence": 0.5}
    assert doc["calculation"]["rules"]["before_seconds"] == 10.0 and doc["calculation"]["captured_at"] == iso(120)
    assert doc["periods"]["before"] == {"start": iso(0), "end": iso(10), "start_inclusive": True, "end_inclusive": False}
    assert doc["periods"]["after"] == {"start": iso(30), "end": iso(60), "start_inclusive": False, "end_inclusive": True}
    live = client.get(f"{REC}/{scene['rec']}/athletes/a1/effort-review", params={"effort_start": iso(10), "effort_end": iso(30)}).json()
    RecordingEffortReview.model_validate(doc["review"])
    assert doc["review"]["movement"] == live["movement"] and doc["review"]["heart_rate"] == live["heart_rate"] and doc["review"]["session_availability"] == live["session_availability"]
    # samples: physiological across the whole before..after span, tracking within the effort, as stored
    phys, track = doc["samples"]["physiological"], doc["samples"]["tracking"]
    assert len(phys) == 40 and phys[0]["timestamp"] == iso(0) and phys[-1]["timestamp"] == iso(39), "before [0,10) during [10,30] after (30,60]: every recorded physiological sample lies in the span"
    assert len(track) == 21 and track[0]["timestamp"] == iso(10) and track[-1]["timestamp"] == iso(30)
    assert sum(1 for s in phys if s["heart_rate_bpm"] is None) == 6 and any(s["speed_mps"] == 0.0 for s in track) and any(s["tracking_confidence"] == 0.2 for s in track)
    assert all(s["source"] == "camera" and s["device_id"] == "cam-2" for s in track) and all(s["source"] == "wearable" and s["device_id"] == "band-9" for s in phys)
    assert doc["review"]["heart_rate"]["during"]["sample_count"] == 21 and doc["review"]["heart_rate"]["during"]["heart_rate_count"] == 18, "three null heart rates in [10, 30] count as samples, not measurements"
    assert doc["review"]["movement"]["excluded_interval_count"] == 2, "the low-confidence point excludes the interval on each side"
    assert doc["review"]["movement"]["sources"] == {"reported": {"camera": 21}, "not_reported": 0}
    def keys(o):
        if isinstance(o, dict):
            for k, v in o.items():
                yield k.lower(); yield from keys(v)
        elif isinstance(o, list):
            for v in o:
                yield from keys(v)
    assert not any(w in k for k in keys(doc) for w in ("fatigue", "readiness", "diagnos", "recover", "score", "substitut", "baseline")), "no interpretive field exists; the prose only says so"


def test_a_changed_setting_changes_a_new_export_but_not_the_saved_bounds_and_an_earlier_file_stands(client, scene, settings_env, frozen):
    _, first = export(client, scene)
    settings_env(HIGH_SPEED_THRESHOLD_MPS="4.0", DISTANCE_MAX_GAP_SECONDS="2.0")
    frozen["now"] = at(180)
    _, second = export(client, scene)
    assert second["saved_period"] == first["saved_period"], "the selection is unchanged"
    assert second["calculation"]["distance_rules"]["high_speed_threshold_mps"] == 4.0 and first["calculation"]["distance_rules"]["high_speed_threshold_mps"] == 5.0
    assert second["review"]["movement"]["high_speed_duration_seconds"] > first["review"]["movement"]["high_speed_duration_seconds"], "a lower threshold counts more time as high-speed"
    assert second["review"]["movement"]["distance_rules"]["high_speed_threshold_mps"] == 4.0 and second["generated_at"] == iso(180)
    assert first["generated_at"] == iso(120) and first["review"]["movement"]["distance_rules"]["high_speed_threshold_mps"] == 5.0, "the earlier document is a standalone record of the earlier calculation"
    assert second["samples"] == first["samples"], "the same stored samples; only the rules differ"


def test_export_404s_and_empty_periods(client, scene, frozen):
    base = f"{REC}/{scene['rec']}/athletes"
    assert client.get(f"{base}/a1/periods/per-nope/review.json").status_code == 404
    assert client.get(f"{base}/a2/periods/{scene['period']['id']}/review.json").status_code == 404
    assert client.get(f"{REC}/rec-nope/athletes/a1/periods/{scene['period']['id']}/review.json").status_code == 404
    empty = client.post(f"{base}/a1/periods", json={"label": "nothing here", "effort_start": iso(100), "effort_end": iso(110)}).json()
    _, doc = export(client, scene, empty["id"])
    assert doc["review"]["movement"]["sample_count"] == 0 and doc["review"]["movement"]["estimated_distance_m"] is None and doc["samples"] == {"physiological": [], "tracking": [], "note": doc["samples"]["note"]}
    assert all(doc["review"]["heart_rate"][n]["sample_count"] == 0 for n in ("before", "during", "after"))
    jsonl = client.get(f"{base}/a1/recorded-samples.jsonl")
    assert jsonl.headers["content-type"].startswith("application/x-ndjson") and all(set(json.loads(l)) == {"type", "sample"} for l in jsonl.text.splitlines()), "the interchange export is untouched"
