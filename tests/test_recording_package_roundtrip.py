"""Every legitimate stopped recording this engine produces must export and import intact.

Membership is decided by acceptance while the recording is active, not by sample timestamps, so an engine-produced
package can hold a delayed sample timestamped before started_at and a sample accepted just before the stop with a
timestamp up to the 5 s future allowance after stopped_at. The importer's only timestamp rule (nothing later than
stopped_at + 5 s) is exactly the bound live ingestion implies, so these packages must import. All data is fictional.
"""

import importlib.util
import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from app.core.recording_package import FORMAT_VERSION as PV

from app.core import clock
from app.core.config import get_settings
from app.core.recording_package import read_package
from app.db import Database
from app.store.recording_import import import_package

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("recording_package_cli_rt", ROOT / "scripts" / "recording_package.py")
cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)
NOW = datetime(2026, 10, 26, 20, 0, 0, tzinfo=UTC)
REC = "/api/v1/recordings"


def at(s):
    return NOW + timedelta(seconds=s)


@pytest.fixture
def frozen(monkeypatch):
    state = {"now": NOW}
    monkeypatch.setattr(clock, "utc_now", lambda: state["now"])
    return state


@pytest.fixture
def small_caps(monkeypatch):
    monkeypatch.setenv("PHYSIOLOGICAL_RETENTION_SAMPLES", "5")
    monkeypatch.setenv("TRACKING_RETENTION_SAMPLES", "5")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def phys(client, a, ts, **extra):
    r = client.post("/api/v1/samples/physiological", json={"athlete_id": a, "timestamp": ts if isinstance(ts, str) else ts.isoformat(), "source": "wearable", "device_id": "w", **extra})
    assert r.status_code == 200, r.text


def track(client, a, ts, **extra):
    r = client.post("/api/v1/samples/tracking", json={"athlete_id": a, "timestamp": ts if isinstance(ts, str) else ts.isoformat(), "x_position": 0, "y_position": 0, "speed_mps": 1.0, "acceleration_mps2": 0, "tracking_confidence": 1.0, **extra})
    assert r.status_code == 200, r.text


def test_engine_produced_edge_cases_round_trip_exactly(client, frozen, small_caps, tmp_path):
    for n, a in enumerate(("a1", "a2", "a3"), start=1):
        assert client.post("/api/v1/athletes", json={"id": a, "name": f"Fictional {a}", "jersey_number": n, "sport": "fictional", "team_id": "t"}).status_code == 201
    rec = client.post(REC, json={"name": "edges", "athlete_ids": ["a1", "a2", "a3"]}).json()["id"]   # started_at = NOW
    phys(client, "a1", at(-3600), heart_rate_bpm=70)                       # delayed historical sample, an hour before started_at
    for s in range(1, 321):                                                # more than the rolling caps (5) and the 300-row history page
        frozen["now"] = at(s)
        track(client, "a1", at(s), speed_mps=0.0 if s == 2 else 2.0, tracking_confidence=0.0 if s == 3 else 1.0)
        phys(client, "a1", at(s), heart_rate_bpm=None if s == 4 else 100, breathing_rate_bpm=0.0 if s == 5 else None, spo2_percent=97)
    phys(client, "a1", at(10).astimezone(timezone(timedelta(hours=-7))).isoformat(), heart_rate_bpm=111)   # same instant, other offset: replaces
    phys(client, "a2", at(320).isoformat(), heart_rate_bpm=90)            # a2 has physiology only; a3 has nothing at all
    phys(client, "a2", at(325), heart_rate_bpm=91)                         # accepted at 320 s, timestamped 5 s ahead: the allowance's limit
    track(client, "a2", at(324.5))                                         # 4.5 s ahead
    stopped = client.post(f"{REC}/{rec}/stop").json()                      # stopped_at = 320 s: two samples lie after it, legitimately
    assert stopped["stopped_at"] == at(320).isoformat().replace("+00:00", "Z")
    frozen["now"] = at(400)
    for label in ("same label", "same label"):
        assert client.post(f"{REC}/{rec}/athletes/a1/periods", json={"label": label, "effort_start": at(5).isoformat(), "effort_end": at(15).isoformat()}).status_code == 201
    pkg_bytes = client.get(f"{REC}/{rec}/package.zip").content
    pkg = read_package(pkg_bytes)                                           # the importer's full validation accepts it
    s = pkg.summary()
    assert s["counts"] == {"physiological_samples": 323, "tracking_samples": 321, "periods": 2, "receipts": 644, "context_entries": 0, "performance_observations": 0}
    assert [a["physiological"]["count"] for a in s["athletes"]] == [321, 2, 0] and s["athletes"][2]["tracking"]["count"] == 0
    assert s["athletes"][0]["physiological"]["bounds"]["earliest"] == "2026-10-26T19:00:00Z", "a sample before started_at is kept"
    assert s["athletes"][1]["physiological"]["bounds"]["latest"] == "2026-10-26T20:05:25Z", "a sample after stopped_at, within the allowance, is kept"
    path = tmp_path / "edges.zip"; path.write_bytes(pkg_bytes)
    dest = tmp_path / "dest.db"
    lines = []
    assert cli.main(["import", str(path), "--database", str(dest), "--json"], out=lines.append) == 0, lines
    new_id = json.loads("\n".join(lines))["recording_id"]
    db = Database(dest)
    try:
        with db.read() as c:
            def payloads(table, rid, a):
                return [r[0] for r in c.execute(f"SELECT payload FROM {table} WHERE recording_id = ? AND athlete_id = ? ORDER BY ts_utc", (rid, a)).fetchall()]
            assert c.execute("SELECT COUNT(*) FROM recording_physiological_samples").fetchone()[0] == 323
            replaced = [json.loads(p) for p in payloads("recording_physiological_samples", new_id, "a1") if json.loads(p)["heart_rate_bpm"] == 111]
            assert len(replaced) == 1 and replaced[0]["timestamp"] == "2026-10-26T13:00:10-07:00", "the replacement kept its own offset; one row for the instant"
            stored = [json.loads(p) for p in payloads("recording_physiological_samples", new_id, "a1")]
            assert stored[0]["timestamp"] == "2026-10-26T19:00:00Z" and any(p["heart_rate_bpm"] is None for p in stored) and any(p.get("breathing_rate_bpm") == 0.0 for p in stored)
            trk = [json.loads(p) for p in payloads("recording_tracking_samples", new_id, "a1")]
            assert len(trk) == 320 and any(p["speed_mps"] == 0.0 for p in trk) and any(p["tracking_confidence"] == 0.0 for p in trk)
            assert payloads("recording_physiological_samples", new_id, "a3") == [] and c.execute("SELECT COUNT(*) FROM recording_athletes WHERE recording_id = ?", (new_id,)).fetchone()[0] == 3, "an athlete with no samples keeps its roster entry"
            assert c.execute("SELECT COUNT(*) FROM recording_periods WHERE recording_id = ? AND label = 'same label'", (new_id,)).fetchone()[0] == 2
    finally:
        db.close()


def test_the_timestamp_rule_is_exactly_the_live_bound(client, frozen, tmp_path):
    """stopped_at + 5 s is accepted (a live engine could have taken it); anything later cannot come from a live engine and is rejected."""
    assert client.post("/api/v1/athletes", json={"id": "a1", "name": "Fictional a1", "jersey_number": 1, "sport": "fictional", "team_id": "t"}).status_code == 201
    rec = client.post(REC, json={"name": "bound", "athlete_ids": ["a1"]}).json()["id"]
    frozen["now"] = at(10)
    phys(client, "a1", at(15), heart_rate_bpm=90)                            # exactly 5 s ahead at acceptance
    client.post(f"{REC}/{rec}/stop")                                         # stopped at 10 s
    pkg = read_package(client.get(f"{REC}/{rec}/package.zip").content)
    assert pkg.summary()["counts"]["physiological_samples"] == 1
    from app.core.recording_package import PackageError, build_package
    rec_doc = {k: pkg.recording[k] for k in ("id", "name", "status", "started_at", "stopped_at")}
    late = pkg.samples["a1"]["physiological"][0].model_copy(update={"timestamp": at(15.001)})
    bad = build_package(rec_doc, pkg.athletes, {"a1": {"physiological": [late], "tracking": []}}, [], {}, at(20))
    with pytest.raises(PackageError, match="after the recording stopped"):
        read_package(bad)
