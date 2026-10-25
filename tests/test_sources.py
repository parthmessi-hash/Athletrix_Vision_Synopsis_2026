"""Source information: optional tracking source/device_id, backward compatibility, and the per-stream source breakdown.

A source is metadata supplied by the sender (a generic category and a device identifier). The engine stores it, counts
it and echoes it; it never infers a source from values, file names or delivery path, and never treats "simulated" or any
other label as a statement about authenticity or sensor accuracy. All data below is fictional.
"""

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core import clock
from app.core.sources import source_breakdown
from app.core.summary import summarize_window
from app.db import to_utc_iso
from app.models import AthleteWindow, PhysiologicalSample, SourceBreakdown, TrackingRules, TrackingSample
from app.store.tracking import TrackingStore

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
PHYS = "/api/v1/samples/physiological"
TRACK = "/api/v1/samples/tracking"
TRK = {"x_position": 1.0, "y_position": 2.0, "speed_mps": 3.0, "acceleration_mps2": 0.0, "tracking_confidence": 0.9}


def at(seconds: float) -> datetime:
    return NOW + timedelta(seconds=seconds)


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    monkeypatch.setattr(clock, "utc_now", lambda: NOW)


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---- TrackingSample: optional, backward-compatible source and device_id ---------------------------------------

def test_tracking_sample_without_source_is_valid_and_means_not_reported():
    s = TrackingSample(athlete_id="a", timestamp=at(0), **TRK)
    assert s.source is None and s.device_id is None
    assert json.loads(s.model_dump_json())["source"] is None


def test_tracking_sample_accepts_existing_source_values_and_rejects_vendor_names_and_empty_device_id():
    s = TrackingSample(athlete_id="a", timestamp=at(0), source="camera", device_id="cam-7", **TRK)
    assert s.source.value == "camera" and s.device_id == "cam-7"
    for bad in ({"source": "acme-cam"}, {"source": ""}, {"device_id": ""}):
        with pytest.raises(ValidationError):
            TrackingSample(athlete_id="a", timestamp=at(0), **TRK, **bad)


def test_stored_tracking_rows_written_before_the_fields_existed_still_read(isolated_db):
    """A legacy payload in SQLite has no source/device_id keys at all; it must load and report not_reported."""
    legacy = {"athlete_id": "old", "timestamp": "2026-10-20T10:00:00Z", **TRK}
    with isolated_db.transaction() as c:
        c.execute("INSERT INTO tracking_samples (athlete_id, ts_utc, payload) VALUES (?, ?, ?)",
                  ("old", to_utc_iso(datetime(2026, 10, 20, 10, 0, 0, tzinfo=UTC)), json.dumps(legacy)))
    store = TrackingStore(db=isolated_db)
    sample = store.get("old")
    assert sample is not None and sample.source is None and sample.device_id is None and sample.speed_mps == 3.0
    assert source_breakdown([sample]).model_dump() == {"reported": {}, "not_reported": 1}


def test_ingest_accepts_tracking_with_and_without_source_and_echoes_it(client):
    old = client.post(TRACK, json={"athlete_id": "a", "timestamp": at(-2).isoformat(), **TRK})
    new = client.post(TRACK, json={"athlete_id": "a", "timestamp": at(-1).isoformat(), "source": "wearable", "device_id": "unit-1", **TRK})
    assert old.status_code == 200 and old.json()["source"] is None and old.json()["device_id"] is None
    assert new.status_code == 200 and new.json()["source"] == "wearable" and new.json()["device_id"] == "unit-1"
    hist = client.get("/api/v1/athletes/a/tracking/history?limit=10").json()
    assert [s["source"] for s in hist] == [None, "wearable"]
    assert client.post(TRACK, json={"athlete_id": "a", "timestamp": at(0).isoformat(), "source": "acme-cam", **TRK}).status_code == 422


# ---- breakdown: shared pure function ------------------------------------------------------------------------------

def phys(sec, source="simulated", hr=100.0):
    return PhysiologicalSample(athlete_id="a", timestamp=at(sec), source=source, device_id="d", heart_rate_bpm=hr)


def trk(sec, source=None, device_id=None):
    return TrackingSample(athlete_id="a", timestamp=at(sec), source=source, device_id=device_id, **TRK)


def test_breakdown_counts_every_sample_including_null_heart_rates_and_adds_up():
    samples = [phys(-5, "wearable", None), phys(-4, "wearable", 101), phys(-3, "simulated", None), phys(-2, "manual", 99)]
    b = source_breakdown(samples)
    assert b.model_dump() == {"reported": {"manual": 1, "simulated": 1, "wearable": 2}, "not_reported": 0}
    assert b.total == len(samples)


def test_breakdown_mixed_and_missing_tracking_sources():
    samples = [trk(-4), trk(-3, "camera", "cam-1"), trk(-2), trk(-1, "camera", "cam-2"), trk(0, "simulated", "sim")]
    b = source_breakdown(samples)
    assert b.model_dump() == {"reported": {"camera": 2, "simulated": 1}, "not_reported": 2}
    assert b.total == 5
    assert source_breakdown([]).model_dump() == {"reported": {}, "not_reported": 0}


def test_breakdown_model_rejects_zero_or_negative_counts_and_unknown_sources():
    with pytest.raises(ValidationError):
        SourceBreakdown(reported={"wearable": 0})
    with pytest.raises(ValidationError):
        SourceBreakdown(not_reported=-1)
    with pytest.raises(ValidationError):
        SourceBreakdown(reported={"acme": 1})


# ---- window and summary carry the breakdown of exactly the selected samples -------------------------------------------

def test_window_derives_sources_from_its_own_sample_lists():
    w = AthleteWindow(athlete_id="a", window_start=at(-60), window_end=at(0), tracking_rules=TrackingRules(min_confidence=0.5),
                      physiological_samples=[phys(-1, "wearable", None)], tracking_samples=[trk(-1), trk(0, "camera", "c")])
    assert w.sources.physiological.model_dump() == {"reported": {"wearable": 1}, "not_reported": 0}
    assert w.sources.tracking.model_dump() == {"reported": {"camera": 1}, "not_reported": 1}
    s = summarize_window(w)
    assert s.physiological.sources == w.sources.physiological and s.tracking.sources == w.sources.tracking
    assert s.physiological.sources.total == s.physiological.sample_count and s.tracking.sources.total == s.tracking.sample_count


def post(client, path, body):
    r = client.post(path, json=body)
    assert r.status_code == 200, r.text


def test_endpoints_report_breakdowns_for_the_selected_window_only(client):
    # physiological: 2 wearable (one with a null heart rate), 1 simulated inside the window; 1 manual outside it
    post(client, PHYS, {"athlete_id": "a", "timestamp": at(-50).isoformat(), "source": "wearable", "device_id": "w", "heart_rate_bpm": None})
    post(client, PHYS, {"athlete_id": "a", "timestamp": at(-40).isoformat(), "source": "wearable", "device_id": "w", "heart_rate_bpm": 120})
    post(client, PHYS, {"athlete_id": "a", "timestamp": at(-30).isoformat(), "source": "simulated", "device_id": "sim", "heart_rate_bpm": 121})
    post(client, PHYS, {"athlete_id": "a", "timestamp": at(-90).isoformat(), "source": "manual", "device_id": "m", "heart_rate_bpm": 90})
    # tracking: 1 camera, 2 without a source inside the window; 1 camera outside it
    post(client, TRACK, {"athlete_id": "a", "timestamp": at(-20).isoformat(), "source": "camera", "device_id": "cam", **TRK})
    post(client, TRACK, {"athlete_id": "a", "timestamp": at(-10).isoformat(), **TRK})
    post(client, TRACK, {"athlete_id": "a", "timestamp": at(0).isoformat(), **TRK})
    post(client, TRACK, {"athlete_id": "a", "timestamp": at(-70).isoformat(), "source": "camera", "device_id": "cam", **TRK})
    win = client.get("/api/v1/athletes/a/window?seconds=60").json()
    assert win["sources"] == {"physiological": {"reported": {"simulated": 1, "wearable": 2}, "not_reported": 0},
                              "tracking": {"reported": {"camera": 1}, "not_reported": 2}}
    assert len(win["physiological_samples"]) == 3 and len(win["tracking_samples"]) == 3
    summary = client.get("/api/v1/athletes/a/summary?seconds=60").json()
    p, t = summary["physiological"], summary["tracking"]
    assert p["sources"] == win["sources"]["physiological"] and t["sources"] == win["sources"]["tracking"]
    assert p["sample_count"] == 3 and p["heart_rate_count"] == 2 and sum(p["sources"]["reported"].values()) + p["sources"]["not_reported"] == 3
    assert t["sample_count"] == 3 and sum(t["sources"]["reported"].values()) + t["sources"]["not_reported"] == 3
    assert t["estimated_distance_m"] is None and t["excluded_interval_count"] == 2 and t["speed_peak_mps"] == 3.0  # 10 s gaps exceed the 3 s rule; existing metrics unchanged by sources
    # a recorded review of the older samples sees only them
    old = client.get(f"/api/v1/athletes/a/summary?seconds=30&end={at(-65).isoformat().replace('+', '%2B')}").json()
    assert old["physiological"]["sources"] == {"reported": {"manual": 1}, "not_reported": 0}
    assert old["tracking"]["sources"] == {"reported": {"camera": 1}, "not_reported": 0}


def test_empty_window_has_empty_breakdowns(client):
    win = client.get("/api/v1/athletes/nobody/window?seconds=60").json()
    assert win["sources"] == {"physiological": {"reported": {}, "not_reported": 0}, "tracking": {"reported": {}, "not_reported": 0}}
    s = client.get("/api/v1/athletes/nobody/summary?seconds=60").json()
    assert s["physiological"]["sources"] == {"reported": {}, "not_reported": 0} and s["tracking"]["sources"] == {"reported": {}, "not_reported": 0}


# ---- converter, replay and simulator preserve supplied source information -------------------------------------------------

class Borrowed:
    def __init__(self, client):
        self.client = client

    def __enter__(self):
        return self.client

    def __exit__(self, *exc):
        return False


def test_replay_preserves_tracking_source_and_accepts_older_files_without_it(client, tmp_path):
    rp = load("replay_samples")
    legacy = tmp_path / "legacy.jsonl"
    legacy.write_text(json.dumps({"type": "tracking", "sample": {"athlete_id": "r", "timestamp": "2026-10-20T10:00:00Z", **TRK}}) + "\n"
                      + json.dumps({"type": "tracking", "sample": {"athlete_id": "r", "timestamp": "2026-10-20T10:00:01Z", "source": "camera", "device_id": "cam-9", **TRK}}) + "\n", encoding="utf-8")
    code = rp.main([str(legacy)], make_client=lambda url: Borrowed(client), clock=lambda: 0.0, sleep=lambda s: None, out=lambda s: None)
    assert code == 0
    hist = client.get("/api/v1/athletes/r/tracking/history?limit=10").json()
    assert [(s["source"], s["device_id"]) for s in hist] == [(None, None), ("camera", "cam-9")]  # nothing inferred for the older record
    win = client.get("/api/v1/athletes/r/window?seconds=60&end=2026-10-20T10:00:01Z").json()
    assert win["sources"]["tracking"] == {"reported": {"camera": 1}, "not_reported": 1}


def test_fixture_tracking_records_state_simulated_explicitly():
    rp = load("replay_samples")
    recs, errors = rp.load_file(ROOT / "scripts" / "examples" / "fictional_replay.jsonl")
    assert errors == []
    tracking = [r.payload for r in recs if r.type == "tracking"]
    assert tracking and all(p["source"] == "simulated" and p["device_id"].startswith("fictional-recorder-") for p in tracking)


def test_tracking_converter_writes_the_given_source_and_optional_device_id(tmp_path):
    ct = load("convert_tracking_csv")
    fixture = ROOT / "scripts" / "examples" / "fictional_tracking.csv"
    out = []
    assert ct.main([str(fixture), str(tmp_path / "a.jsonl"), "--athlete-id", "x", "--source", "simulated"], out=out.append) == 0
    recs = [json.loads(l) for l in (tmp_path / "a.jsonl").read_text().splitlines()]
    assert all(r["sample"]["source"] == "simulated" and "device_id" not in r["sample"] for r in recs)
    assert ct.main([str(fixture), str(tmp_path / "b.jsonl"), "--athlete-id", "x", "--source", "wearable", "--device-id", "unit-3"], out=out.append) == 0
    recs = [json.loads(l) for l in (tmp_path / "b.jsonl").read_text().splitlines()]
    assert all(r["sample"]["source"] == "wearable" and r["sample"]["device_id"] == "unit-3" for r in recs)  # a label, not a claim: the rows are fictional


def test_simulator_marks_generated_tracking_as_simulated_with_its_device_id():
    source = (ROOT / "scripts" / "simulate_athlete.py").read_text(encoding="utf-8")
    tracking_block = source[source.index("tracking = {"):source.index("line += (")]
    assert '"source": "simulated"' in tracking_block and '"device_id": device_id' in tracking_block
