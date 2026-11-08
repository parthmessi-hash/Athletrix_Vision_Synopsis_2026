"""The fatigue pilot's study workflow end to end on a frozen clock (FICTIONAL participants P1 and P2 and made-up
readings): session with a frozen profile, wearer assignments, rounds, the engine-saved prediction at each sprint cutoff,
retries, corrections with history, a wearer change that inherits nothing, the leakage regression (a changed sprint time
and sprint readings, and a reading received after the cutoff, leave the saved prediction unchanged; excluded sprint
readings never reach the next round), and export, import and reproduction."""

import random
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.api import study as study_api
from app.core import clock
from app.core.config import Settings
from app.core.recording_package import read_package
from app.db import Database, set_database
from app.store.recording_import import import_package

ROOT = Path(__file__).resolve().parents[1]
PROFILE = str(ROOT / "scripts" / "examples" / "pilot" / "fictional_fatigue_profile.json")
T0 = datetime(2026, 11, 7, 9, 0, tzinfo=UTC)
API = "/api/v1"


def z(t):
    return t.isoformat().replace("+00:00", "Z")


@pytest.fixture
def frozen(monkeypatch):
    st = {"now": T0}
    monkeypatch.setattr(clock, "utc_now", lambda: st["now"])
    monkeypatch.setattr(clock, "receipt_now", lambda: st["now"])
    monkeypatch.setattr(study_api, "get_settings", lambda: Settings(fatigue_profile_path=PROFILE))
    return st


def hr_body(aid, t, hr, dev):
    return {"athlete_id": aid, "timestamp": z(t), "source": "simulated", "device_id": dev, "heart_rate_bpm": round(hr, 1)}


def tr_body(aid, t, v):
    return {"athlete_id": aid, "timestamp": z(t), "x_position": 0.0, "y_position": 0.0, "speed_mps": round(v, 2), "acceleration_mps2": 0.0, "tracking_confidence": 0.9,
            "source": "camera", "segment_id": f"seg-{aid}", "value_basis": "estimated"}


class Sim:
    def __init__(self, client, st):
        self.c, self.st, self.rng = client, st, random.Random("pilot-test")

    def run(self, until, aid, dev, hr=lambda s: 140, v=lambda s: 3.0, t0=None):
        """Advance the clock one 0.2 s step at a time, sending 1 Hz heart rate and 5 Hz camera speed for aid."""
        while self.st["now"] < until:
            self.st["now"] += timedelta(seconds=0.2)
            s = (self.st["now"] - (t0 or T0)).total_seconds()
            if round(s * 5) % 5 == 0:
                self.c.post(f"{API}/samples/physiological", json=hr_body(aid, self.st["now"], hr(s) + self.rng.gauss(0, 1.5), dev))
            self.c.post(f"{API}/samples/tracking", json=tr_body(aid, self.st["now"], max(0.0, v(s) + self.rng.gauss(0, 0.1))))


def entry(c, rid, **kw):
    r = c.post(f"{API}/recordings/{rid}/study", json=kw)
    assert r.status_code in (200, 201), r.text
    return r.json()


@pytest.fixture
def pilot(client, frozen):
    sim = Sim(client, frozen)
    for i, aid in enumerate(("x1", "x2")):
        client.post(f"{API}/athletes", json={"id": aid, "name": f"Fictional participant {i + 1}", "jersey_number": i + 1, "sport": "soccer", "team_id": "fictional-study"})
    rest = client.post(f"{API}/recordings", json={"name": "FICTIONAL rest", "athlete_ids": ["x1", "x2"]}).json()["id"]
    r0 = frozen["now"]
    for k in range(150):
        frozen["now"] = r0 + timedelta(seconds=k)
        for aid, dev in (("x1", "strap-A"), ("x2", "strap-A")):
            client.post(f"{API}/samples/physiological", json=hr_body(aid, frozen["now"], 62 + sim.rng.gauss(0, 3), dev))
    frozen["now"] += timedelta(seconds=1)
    client.post(f"{API}/recordings/{rest}/stop")
    base = {}
    for aid in ("x1", "x2"):
        b = client.post(f"{API}/athletes/{aid}/baselines", json={"source_recording_id": rest, "ranges": [{"start": z(r0), "end": z(r0 + timedelta(seconds=149))}],
                                                                "condition": "seated_rest", "signals": ["heart_rate_bpm"], "min_samples": 100, "label": "FICTIONAL", "created_by": "fictional"}).json()
        client.post(f"{API}/athletes/{aid}/baselines/{b['id']}/decision", json={"decision": "accepted", "decided_by": "fictional", "note": "FICTIONAL"})
        base[aid] = b["id"]
    frozen["now"] = T0 + timedelta(minutes=10)
    rid = client.post(f"{API}/recordings", json={"name": "FICTIONAL pilot day 1", "athlete_ids": ["x1", "x2"]}).json()["id"]
    m0 = frozen["now"]
    entry(client, rid, kind="session", client_token="sess-0001", session_label="day1", protocol="rehearsal", fictional=True, planned_rounds=2,
          clock_references=[{"stream": "camera", "basis": "operator_estimate", "uncertainty_s": 0.05, "note": "FICTIONAL"}])
    entry(client, rid, kind="wearer", client_token="wear-x1-0001", athlete_id="x1", participant_code="P1", device_id="strap-A", baseline_id=base["x1"],
          hr_max_bpm=200, hr_max_origin="FICTIONAL: estimated 208 - 0.7 x age")
    entry(client, rid, kind="round_start", client_token="r1-start-x1", athlete_id="x1", round=1)
    return {"c": client, "sim": sim, "rid": rid, "m0": m0, "base": base, "st": frozen}


def test_session_prediction_retry_correction_and_leakage(pilot):
    c, sim, rid, m0, st = pilot["c"], pilot["sim"], pilot["rid"], pilot["m0"], pilot["st"]
    rising = lambda s: 130 + 0.05 * s  # noqa: E731
    sim.run(m0 + timedelta(seconds=300), "x1", "strap-A", hr=rising, t0=m0)
    mark = dict(kind="sprint_start", client_token="r1-sprint-x1", athlete_id="x1", round=1)
    s1 = entry(c, rid, **mark)
    assert c.post(f"{API}/recordings/{rid}/study", json=mark).status_code == 200, "a retried mark is stored once"
    log = c.get(f"{API}/recordings/{rid}/study").json()
    preds = [e for e in log["entries"] if e["kind"] == "prediction"]
    assert len(preds) == 1 and preds[0]["prediction"]["sprint_start_id"] == s1["id"]
    saved = preds[0]["prediction"]["result"]["candidates"]
    assert all(saved[k]["available"] for k in saved), {k: saved[k]["reasons"] for k in saved}
    # the timed sprint: hot readings in both streams, then its end and (later) the independent result
    sim.run(m0 + timedelta(seconds=306), "x1", "strap-A", hr=lambda s: 192, v=lambda s: 7.6, t0=m0)
    entry(c, rid, kind="sprint_end", client_token="r1-sprint-end-x1", athlete_id="x1", round=1)
    sim.run(m0 + timedelta(seconds=620), "x1", "strap-A", hr=rising, t0=m0)
    r1 = entry(c, rid, kind="sprint_result", client_token="r1-result-x1", athlete_id="x1", round=1, time_s=3.41, method="timing_gates", recorded_by="fictional timer")
    fix = entry(c, rid, kind="sprint_result", client_token="r1-result-x1-fix", athlete_id="x1", round=1, time_s=3.38, method="timing_gates",
                supersedes=r1["id"], correction_reason="FICTIONAL: gate read again")
    entry(c, rid, kind="self_rating", client_token="r1-rating-x1", athlete_id="x1", round=1, missing_reason="not_collected")
    rows = {(r["athlete_id"], r["round"]): r for r in c.get(f"{API}/recordings/{rid}/study").json()["rounds"]}
    assert rows[("x1", 1)]["sprint_result"]["time_s"] == 3.38 and rows[("x1", 1)]["sprint_result"]["supersedes"] == r1["id"] and rows[("x1", 1)]["self_rating"]["missing_reason"] == "not_collected"
    with pytest.raises(AssertionError):
        entry(c, rid, kind="sprint_result", client_token="r1-result-x1-fix2", athlete_id="x1", round=1, time_s=3.3, method="stopwatch", supersedes=r1["id"], correction_reason="again")
    # round 2 score before its sprint: excluded sprint readings must not reach it
    before = c.get(f"{API}/recordings/{rid}/study/score", params={"athlete_id": "x1", "at": z(m0 + timedelta(seconds=620))}).json()
    assert before["inputs"]["excluded"]["test_effort"] > 0
    from app.db import get_database
    path = get_database().path
    con = sqlite3.connect(path)
    from app.db import to_utc_iso
    lo, hi = to_utc_iso(m0 + timedelta(seconds=296)), to_utc_iso(m0 + timedelta(seconds=395))
    n = con.execute("UPDATE recording_physiological_samples SET payload = json_set(payload, '$.heart_rate_bpm', 75.0) WHERE ts_utc BETWEEN ? AND ?", (lo, hi)).rowcount
    n += con.execute("UPDATE recording_tracking_samples SET payload = json_set(payload, '$.speed_mps', 0.2) WHERE ts_utc BETWEEN ? AND ?", (lo, hi)).rowcount
    con.commit(); con.close()
    assert n > 300
    after = c.get(f"{API}/recordings/{rid}/study/score", params={"athlete_id": "x1", "at": z(m0 + timedelta(seconds=620))}).json()
    assert {k: v["value"] for k, v in after["candidates"].items()} == {k: v["value"] for k, v in before["candidates"].items()}, "excluded sprint readings never leak into round 2"
    # a reading for before the cutoff, received after it, and a changed sprint time: the saved prediction stands
    c.post(f"{API}/samples/physiological", json=hr_body("x1", m0 + timedelta(seconds=299.5), 199, "strap-A"))
    entry(c, rid, kind="sprint_result", client_token="r1-result-x1-fix3", athlete_id="x1", round=1, time_s=4.9, method="stopwatch", supersedes=fix["id"], correction_reason="FICTIONAL test")
    rep = c.get(f"{API}/recordings/{rid}/study/predictions/{preds[0]['id']}/reproduce").json()
    assert rep["status"] == "reproduced" and rep["saved"] == rep["recalculated"], rep
    again = [e for e in c.get(f"{API}/recordings/{rid}/study").json()["entries"] if e["id"] == preds[0]["id"]][0]
    assert again["prediction"] == preds[0]["prediction"], "never rewritten"
    retro = c.get(f"{API}/recordings/{rid}/study/score", params={"athlete_id": "x1", "at": z(m0 + timedelta(seconds=300)), "knowledge": "retrospective"}).json()
    assert retro["knowledge"] == "retrospective" and retro["inputs"]["excluded"]["received_after_cutoff"] == 0


def test_wearer_change_inherits_nothing_and_export_reproduces(pilot, tmp_path):
    c, sim, rid, m0, st, base = pilot["c"], pilot["sim"], pilot["rid"], pilot["m0"], pilot["st"], pilot["base"]
    sim.run(m0 + timedelta(seconds=300), "x1", "strap-A", t0=m0)
    r = c.post(f"{API}/recordings/{rid}/study", json={"kind": "wearer", "client_token": "wear-x2-bad", "athlete_id": "x2", "participant_code": "P2", "device_id": "strap-A",
                                                     "baseline_id": base["x1"], "hr_max_bpm": 195, "hr_max_origin": "FICTIONAL"})
    assert r.status_code == 422 and "never uses another participant's baseline" in r.json()["detail"]
    entry(c, rid, kind="wearer", client_token="wear-x2-0001", athlete_id="x2", participant_code="P2", device_id="strap-A", baseline_id=base["x2"], hr_max_bpm=195, hr_max_origin="FICTIONAL")
    entry(c, rid, kind="round_start", client_token="r1-start-x2", athlete_id="x2", round=1)
    t_change = st["now"]
    # the adapter keeps tagging the strap with x1 for 20 s by mistake, then x2
    sim.run(t_change + timedelta(seconds=20), "x1", "strap-A", hr=lambda s: 175, t0=m0)
    sim.run(t_change + timedelta(seconds=320), "x2", "strap-A", hr=lambda s: 150, t0=m0)
    x1 = c.get(f"{API}/recordings/{rid}/study/score", params={"athlete_id": "x1"}).json()
    assert x1["candidates"]["vest_only"]["available"] is False and "not the active wearer" in x1["features"]["hr_strain"]["reason"]
    assert x1["inputs"]["excluded"]["outside_own_wearer_assignment"] >= 19
    x2 = c.get(f"{API}/recordings/{rid}/study/score", params={"athlete_id": "x2"}).json()
    assert x2["features"]["hr_strain"]["reference"]["baseline_id"] == base["x2"] and x2["candidates"]["vest_only"]["available"]
    pilot_view = c.get(f"{API}/recordings/{rid}/study/pilot").json()
    assert pilot_view["wearer"]["participant_code"] == "P2" and pilot_view["fictional"] and pilot_view["mode"] == "live" and len(pilot_view["trend"]) >= 2
    assert "substitution" not in str(pilot_view["estimate"]["candidates"]).lower()
    s2 = entry(c, rid, kind="sprint_start", client_token="r1-sprint-x2", athlete_id="x2", round=1)
    st["now"] += timedelta(seconds=10)
    c.post(f"{API}/recordings/{rid}/stop")
    data = c.get(f"{API}/recordings/{rid}/package.zip").content
    pkg = read_package(data)
    assert pkg.manifest["format_version"] == 8 and [e.kind for e in pkg.study].count("prediction") == 1 and pkg.study[0].kind == "session"
    pred = next(e for e in pkg.study if e.kind == "prediction")
    assert pred.prediction["sprint_start_id"] == s2["id"]
    other = Database(tmp_path / "other.db")
    from app.db import get_database
    mine = get_database()
    try:
        new = import_package(other, pkg, st["now"]).recording_id
        set_database(other)
        log = c.get(f"{API}/recordings/{new}/study").json()
        p2 = next(e for e in log["entries"] if e["kind"] == "prediction")
        rep = c.get(f"{API}/recordings/{new}/study/predictions/{p2['id']}/reproduce").json()
        assert rep["status"] == "reproduced", rep
        assert p2["prediction"]["sprint_start_id"] in {e["id"] for e in log["entries"]}, "references re-pointed to the imported copies"
    finally:
        set_database(mine)
        other.close()


def test_backdated_marker_same_instant_order_and_retry_with_another_time(pilot, tmp_path):
    c, sim, rid, m0, st = pilot["c"], pilot["sim"], pilot["rid"], pilot["m0"], pilot["st"]
    sim.run(m0 + timedelta(seconds=300), "x1", "strap-A", t0=m0)
    late = entry(c, rid, kind="sprint_start", client_token="r1-sprint-late", athlete_id="x1", round=1, at=z(m0 + timedelta(seconds=290)))
    pred = next(e for e in c.get(f"{API}/recordings/{rid}/study").json()["entries"] if e["kind"] == "prediction" and e["prediction"]["sprint_start_id"] == late["id"])
    assert pred["prediction"]["prospective"] is False and pred["prediction"]["marker_lag_s"] == 10.0 and pred["prediction"]["timing_label"].startswith("RETROSPECTIVE")
    r = c.post(f"{API}/recordings/{rid}/study", json={"kind": "sprint_start", "client_token": "r1-sprint-late", "athlete_id": "x1", "round": 1, "at": z(m0 + timedelta(seconds=295))})
    assert r.status_code == 409, "the same token with another stated time is refused, never a second prediction"
    assert sum(1 for e in c.get(f"{API}/recordings/{rid}/study").json()["entries"] if e["kind"] == "prediction") == 1
    bad = c.post(f"{API}/recordings/{rid}/study", json={"kind": "round_end", "client_token": "r1-end-invalid", "athlete_id": "x1", "round": 1, "invalid": True})
    assert bad.status_code == 422, "an invalid round needs its reason"
    entry(c, rid, kind="round_end", client_token="r1-end-invalid2", athlete_id="x1", round=1, invalid=True, notes="FICTIONAL: false start")
    for k in range(3):   # three notes at the same instant: their order must survive export and import
        entry(c, rid, kind="note", client_token=f"same-instant-{k}", notes=f"note {k}")
    before = [e["client_token"] for e in c.get(f"{API}/recordings/{rid}/study").json()["entries"]]
    st["now"] += timedelta(seconds=5)
    c.post(f"{API}/recordings/{rid}/stop")
    pkg = read_package(c.get(f"{API}/recordings/{rid}/package.zip").content)
    other = Database(tmp_path / "o.db")
    from app.db import get_database
    mine = get_database()
    try:
        new = import_package(other, pkg, st["now"]).recording_id
        set_database(other)
        after = [e["client_token"] for e in c.get(f"{API}/recordings/{new}/study").json()["entries"]]
        rep = c.get(f"{API}/recordings/{new}/study/predictions/{[e for e in c.get(f'{API}/recordings/{new}/study').json()['entries'] if e['kind'] == 'prediction'][0]['id']}/reproduce").json()
    finally:
        set_database(mine)
        other.close()
    assert after == before, "same-instant entries keep their receipt order through export and import"
    assert rep["status"] == "reproduced" and rep["prospective"] is False
