"""GET /recordings/{id}/package.zip: a portable package of one stopped recording, read in one snapshot. All data is fictional."""

import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime, timedelta

import pytest

from app.core import clock
from app.core.config import get_settings

NOW = datetime(2026, 10, 26, 20, 0, 0, tzinfo=UTC)
REC = "/api/v1/recordings"


@pytest.fixture
def frozen(monkeypatch):
    state = {"now": NOW}
    monkeypatch.setattr(clock, "utc_now", lambda: state["now"])
    return state


@pytest.fixture
def small_caps(monkeypatch):
    monkeypatch.setenv("PHYSIOLOGICAL_RETENTION_SAMPLES", "3")
    monkeypatch.setenv("TRACKING_RETENTION_SAMPLES", "3")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def at(s):
    return NOW + timedelta(seconds=s)


@pytest.fixture
def stopped(client, frozen, small_caps):
    for n, a in enumerate(("a1", "a2", "a3"), start=1):
        assert client.post("/api/v1/athletes", json={"id": a, "name": f"Fictional {a}", "jersey_number": n, "sport": "fictional", "team_id": "t"}).status_code == 201
    other = client.post(REC, json={"name": "earlier", "athlete_ids": ["a1"]}).json()["id"]
    frozen["now"] = at(1)
    assert client.post("/api/v1/samples/physiological", json={"athlete_id": "a1", "timestamp": at(0).isoformat(), "source": "manual", "device_id": "x", "heart_rate_bpm": 61}).status_code == 200
    client.post(f"{REC}/{other}/stop")
    rec = client.post(REC, json={"name": "Tuesday practice", "athlete_ids": ["a2", "a1"]}).json()["id"]
    for s in range(320):                      # beyond the rolling caps (3) and the history page (300)
        frozen["now"] = at(10 + s)
        assert client.post("/api/v1/samples/physiological", json={"athlete_id": "a1", "timestamp": (at(10 + s)).astimezone().isoformat() if s == 5 else at(10 + s).isoformat(),
                                                                   "source": "wearable", "device_id": "band", "heart_rate_bpm": None if s % 50 == 0 else 100 + s % 7, "breathing_rate_bpm": 0.0 if s == 1 else None}).status_code == 200
        assert client.post("/api/v1/samples/tracking", json={"athlete_id": "a1", "timestamp": at(10 + s).isoformat(), "x_position": 0.0, "y_position": 0.0, "speed_mps": 0.0 if s == 3 else 2.0,
                                                              "acceleration_mps2": 0.0, "tracking_confidence": 0.0 if s == 4 else 0.9, "source": "camera", "device_id": "cam"}).status_code == 200
    frozen["now"] = at(400)
    assert client.post("/api/v1/samples/physiological", json={"athlete_id": "a3", "timestamp": at(400).isoformat(), "source": "manual", "device_id": "x", "heart_rate_bpm": 91}).status_code == 200
    client.post(f"{REC}/{rec}/stop")
    for label, a, b in (("sprint", 20, 30), ("sprint", 40, 50)):
        assert client.post(f"{REC}/{rec}/athletes/a1/periods", json={"label": label, "effort_start": at(a).isoformat().replace("+00:00", "Z"), "effort_end": at(b).isoformat().replace("+00:00", "Z")}).status_code == 201
    frozen["now"] = at(500)
    return {"rec": rec, "other": other}


def unzip(body: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(body)) as z:
        return {n: z.read(n) for n in z.namelist()}


def test_package_holds_the_whole_recording_as_stored_with_checksums(client, stopped):
    r = client.get(f"{REC}/{stopped['rec']}/package.zip")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip" and r.headers["X-Athletrix-Format"] == "athletrix-recording-package/2"
    assert r.headers["Content-Disposition"] == f'attachment; filename="athletrix-recording-package-{stopped["rec"]}-20261026T200820Z.zip"'
    files = unzip(r.content)
    assert sorted(files) == ["manifest.json", "periods.json", "recording.json", "samples.jsonl"]
    manifest = json.loads(files["manifest.json"])
    assert manifest["format"] == "athletrix-recording-package" and manifest["format_version"] == 2 and manifest["generated_at"] == "2026-10-26T20:08:20Z"
    for name in ("recording.json", "samples.jsonl", "periods.json"):
        assert manifest["members"][name] == {"sha256": hashlib.sha256(files[name]).hexdigest(), "bytes": len(files[name])}
    assert manifest["counts"] == {"athletes": 2, "physiological_samples": 320, "tracking_samples": 320, "samples_by_athlete": {"a2": {"physiological": 0, "tracking": 0}, "a1": {"physiological": 320, "tracking": 320}}, "periods": 2}
    assert manifest["settings_at_export"]["high_speed_threshold_mps"] == 5.0 and "not necessarily" in manifest["settings_at_export"]["note"]
    rec = json.loads(files["recording.json"])
    assert rec["id"] == stopped["rec"] and rec["status"] == "stopped" and rec["started_at"] == "2026-10-26T20:00:01Z" and rec["stopped_at"] == "2026-10-26T20:06:40Z"
    assert [a["id"] for a in rec["athletes"]] == ["a2", "a1"] and rec["athletes"][1]["name"] == "Fictional a1"
    cfg = rec["configuration_at_start"]
    assert cfg["captured_at"] == "2026-10-26T20:00:01Z" and cfg["movement"]["high_speed_threshold_mps"] == 5.0 and cfg["retention"] == {"physiological_retention_samples": 3, "tracking_retention_samples": 3}
    assert "settings_at_export" not in cfg and cfg["engine"]["code_revision"] is None, "the start-time snapshot is separate from the export-time settings; an unsupplied revision stays unknown"
    lines = [json.loads(line) for line in files["samples.jsonl"].decode().splitlines()]
    assert len(lines) == 640 and all(set(x) == {"type", "sample"} for x in lines), "every recorded sample, beyond the rolling caps and the 300-row history page"
    assert [x["type"] for x in lines[:2]] == ["physiological", "tracking"]
    stored = client.get(f"{REC}/{stopped['rec']}/athletes/a1/recorded-samples.jsonl").text.splitlines()
    assert [json.dumps(x, ensure_ascii=False) for x in lines] == stored, "identical to the existing per-athlete interchange export, byte for byte"
    phys = [x["sample"] for x in lines if x["type"] == "physiological"]
    assert phys[0]["heart_rate_bpm"] is None and phys[1]["breathing_rate_bpm"] == 0.0 and phys[5]["timestamp"] != phys[4]["timestamp"]
    trk = [x["sample"] for x in lines if x["type"] == "tracking"]
    assert trk[3]["speed_mps"] == 0.0 and trk[4]["tracking_confidence"] == 0.0 and trk[0]["source"] == "camera" and trk[0]["device_id"] == "cam"
    assert all(x["sample"]["athlete_id"] != "a3" for x in lines) and not any(x["sample"]["heart_rate_bpm"] == 61 for x in lines if x["type"] == "physiological"), "nothing from another recording or a non-member"
    periods = json.loads(files["periods.json"])
    assert [(p["label"], p["effort_start"], p["effort_end"]) for p in periods] == [("sprint", "2026-10-26T20:00:20Z", "2026-10-26T20:00:30Z"), ("sprint", "2026-10-26T20:00:40Z", "2026-10-26T20:00:50Z")]
    assert len({p["id"] for p in periods}) == 2
    whole = b"".join(files.values()).decode("utf-8", "replace")
    for forbidden in ("/Users/", "/var/folders", ".env", "athletrix.db", "DATABASE_PATH", "CORS_ORIGINS"):
        assert forbidden not in whole, forbidden


def test_active_unknown_and_existing_exports(client, stopped, frozen):
    assert client.get(f"{REC}/rec-nope/package.zip").status_code == 404
    client.post("/api/v1/recordings", json={"name": "live", "athlete_ids": ["a1"]})
    active = client.get(f"{REC}/active").json()["recording"]["id"]
    r = client.get(f"{REC}/{active}/package.zip")
    assert r.status_code == 409 and "still active" in r.json()["detail"]
    other = unzip(client.get(f"{REC}/{stopped['other']}/package.zip").content)
    assert json.loads(other["manifest.json"])["counts"]["physiological_samples"] == 1 and json.loads(other["periods.json"]) == []
    # existing downloads keep their formats
    jsonl = client.get(f"{REC}/{stopped['rec']}/athletes/a1/recorded-samples.jsonl")
    assert jsonl.headers["content-type"].startswith("application/x-ndjson")
    pid = client.get(f"{REC}/{stopped['rec']}/athletes/a1/periods").json()[0]["id"]
    assert json.loads(client.get(f"{REC}/{stopped['rec']}/athletes/a1/periods/{pid}/review.json").text)["format"] == "athletrix-period-review"
