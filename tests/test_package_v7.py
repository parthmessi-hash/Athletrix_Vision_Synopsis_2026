"""Package format 7 (now 8): roster input plans and reviewer fields survive export and import; a version 6 package (no such
fields) still reads, with inputs not stated rather than assumed."""

import io
import json
import zipfile
from datetime import UTC, datetime, timedelta

from app.core import clock
from app.core.recording_package import FORMAT_VERSION, read_package
from app.db import Database
from app.store.recording_import import import_package


def test_v7_round_trip_and_v6_still_reads(client, monkeypatch, tmp_path):
    now = {"t": datetime(2026, 11, 7, 10, tzinfo=UTC)}
    monkeypatch.setattr(clock, "utc_now", lambda: now["t"])
    client.post("/api/v1/athletes", json={"id": "a1", "name": "Fictional a1", "jersey_number": 1, "sport": "soccer", "team_id": "t"})
    rid = client.post("/api/v1/recordings", json={"name": "FICTIONAL", "athlete_ids": ["a1"]}).json()["id"]
    t0 = now["t"]
    assert client.post(f"/api/v1/recordings/{rid}/roster", json={"athlete_id": "a1", "effective_from": t0.isoformat(), "status": "on_field", "current_role": "winger",
                                                                 "inputs": {"physiology": "not_used", "camera": "expected", "camera_interval_s": 0.1}, "source": "assistant_coach"}).status_code == 201
    now["t"] += timedelta(minutes=1)
    assert client.post(f"/api/v1/recordings/{rid}/performance", json={"athlete_id": "a1", "area": "coach_strategy", "kind": "tactical_observation", "period_start": t0.isoformat(),
                                                                      "period_end": (t0 + timedelta(seconds=30)).isoformat(), "assessment": "assessed", "outcome": "followed",
                                                                      "instruction": "press high", "instruction_exception": "drop off when outnumbered", "confidence": "low",
                                                                      "rationale": "fictional"}).status_code == 201
    now["t"] += timedelta(minutes=1)
    client.post(f"/api/v1/recordings/{rid}/stop")
    data = client.get(f"/api/v1/recordings/{rid}/package.zip").content
    pkg = read_package(data)
    assert pkg.manifest["format_version"] == FORMAT_VERSION == 8
    assert pkg.roster[0].inputs.physiology == "not_used" and pkg.performance[0].instruction_exception == "drop off when outnumbered" and pkg.performance[0].confidence == "low"
    db = Database(tmp_path / "other.db")
    try:
        new = import_package(db, pkg, now["t"]).recording_id
        with db.read() as c:
            roster = [json.loads(r["payload"]) for r in c.execute("SELECT payload FROM recording_roster WHERE recording_id = ?", (new,))]
        assert roster[0]["inputs"]["camera_interval_s"] == 0.1
    finally:
        db.close()
    # the same package rewritten as version 6 without the new fields (what an older engine wrote)
    zin = zipfile.ZipFile(io.BytesIO(data))
    files = {n: zin.read(n) for n in zin.namelist()}
    roster6 = [{k: v for k, v in e.items() if k != "inputs"} for e in json.loads(files["roster.json"])]
    perf6 = [{k: v for k, v in o.items() if k not in ("confidence", "rationale", "instruction_exception")} for o in json.loads(files["performance.json"])]
    files["roster.json"] = json.dumps(roster6).encode(); files["performance.json"] = json.dumps(perf6).encode()
    man = json.loads(files["manifest.json"])
    man["format_version"] = 6
    files.pop("study.json"); man["members"].pop("study.json"); man["counts"].pop("study_entries")
    import hashlib
    for name in ("roster.json", "performance.json"):
        man["members"][name] = {"sha256": hashlib.sha256(files[name]).hexdigest(), "bytes": len(files[name])}
    files["manifest.json"] = json.dumps(man).encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n, b in files.items():
            z.writestr(n, b)
    old = read_package(buf.getvalue())
    assert old.manifest["format_version"] == 6 and old.roster[0].inputs is None and old.performance[0].confidence is None, "not stated, never assumed"
