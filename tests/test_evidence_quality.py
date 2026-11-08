"""Missing-data semantics end to end (decision rules 2) and the per-source evidence view, on a frozen clock with the
FICTIONAL live profiles: fresh -> stale -> disconnected -> fresh, a healthy player beside one with missing data, retries,
replacements, rejected writes, a device report, an unknown roster entry, no profile and a stopped recording."""

import random
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.api import review as review_api
from app.core import clock
from app.core.config import Settings
from app.core.evidence import stream_status
from app.models import PhysiologicalSample

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import live_session  # noqa: E402

T0 = datetime(2026, 11, 7, 10, 0, 0, tzinfo=UTC)
API = "/api/v1"


def z(t):
    return t.isoformat().replace("+00:00", "Z")


@pytest.fixture
def frozen(monkeypatch):
    state = {"now": T0}
    monkeypatch.setattr(clock, "utc_now", lambda: state["now"])
    monkeypatch.setattr(clock, "receipt_now", lambda: state["now"])
    return state


@pytest.fixture
def profiles(monkeypatch):
    live = {k: str(v) for k, v in live_session.LIVE_PROFILES.items()}
    s = Settings(physiological_index_profile_path=live["PHYSIOLOGICAL_INDEX_PROFILE_PATH"], substitution_profile_path=live["SUBSTITUTION_PROFILE_PATH"],
                 performance_rubric_path=live["PERFORMANCE_RUBRIC_PATH"])
    monkeypatch.setattr(review_api, "get_settings", lambda: s)
    return s


def hr(aid, t, level=1.0, **kw):
    """Deterministic made-up readings with rest-like spread (level 0 rest, 1 normal play)."""
    g = random.Random(f"{aid}|{t.isoformat()}")
    return {"athlete_id": aid, "timestamp": z(t), "source": "simulated", "device_id": f"strap-{aid}", "heart_rate_bpm": round(62 + 80 * level + g.gauss(0, 3), 1),
            "breathing_rate_bpm": round(14 + 20 * level + g.gauss(0, 1.5), 1), "hrv_rmssd_ms": round(max(3.0, 64 - 38 * level + g.gauss(0, 8)), 1), "signal_quality": 0.92, **kw}


def perf(client, rid, aid, start, end):
    base = {"athlete_id": aid, "role": "centre back", "period_start": z(start), "period_end": z(end), "assessment": "assessed", "annotator": "fictional analyst (simulated)"}
    for extra in ({"area": "execution", "kind": "pass", "attempts": 5, "completions": 4}, {"area": "execution", "kind": "turnover", "possessions": 5, "turnovers": 0},
                  {"area": "decision_making", "kind": "decision", "opportunities": 2, "rated_good": 2, "rated_acceptable": 0, "rated_poor": 0, "criterion": "fictional"},
                  {"area": "movement_positioning", "kind": "positioning", "opportunities": 2, "rated_good": 2, "rated_acceptable": 0, "rated_poor": 0, "criterion": "fictional"},
                  {"area": "coach_strategy", "kind": "tactical_observation", "instruction": "fictional", "outcome": "followed"}):
        assert client.post(f"{API}/recordings/{rid}/performance", json={**base, **extra}).status_code == 201


@pytest.fixture
def session(client, frozen, profiles):
    """Rest recording -> accepted baselines -> match recording with a plan; a1 and a2 wear straps (1 Hz), b1 does not, u1 has no roster entry."""
    for i, aid in enumerate(("a1", "a2", "b1", "u1")):
        assert client.post(f"{API}/athletes", json={"id": aid, "name": f"Fictional {aid}", "jersey_number": i + 1, "sport": "soccer", "team_id": "t"}).status_code == 201
    rest = client.post(f"{API}/recordings", json={"name": "FICTIONAL rest", "athlete_ids": ["a1", "a2"]}).json()["id"]
    start = frozen["now"]
    for s in range(150):
        frozen["now"] = T0 + timedelta(seconds=s)
        for aid in ("a1", "a2"):
            client.post(f"{API}/samples/physiological", json=hr(aid, frozen["now"], 0.0))
    frozen["now"] += timedelta(seconds=1)
    client.post(f"{API}/recordings/{rest}/stop")
    for aid in ("a1", "a2"):
        b = client.post(f"{API}/athletes/{aid}/baselines", json={"source_recording_id": rest, "ranges": [{"start": z(start), "end": z(frozen['now'] - timedelta(seconds=1))}],
                                                                "condition": "seated_rest", "signals": ["heart_rate_bpm", "breathing_rate_bpm", "hrv_rmssd_ms"],
                                                                "min_signal_quality": 0.6, "min_samples": 100, "label": "FICTIONAL", "created_by": "fictional analyst"}).json()
        client.post(f"{API}/athletes/{aid}/baselines/{b['id']}/decision", json={"decision": "accepted", "decided_by": "fictional analyst", "note": "FICTIONAL"})
    frozen["now"] = T0 + timedelta(seconds=300)
    m0 = frozen["now"]
    rid = client.post(f"{API}/recordings", json={"name": "FICTIONAL match", "athlete_ids": ["a1", "a2", "b1", "u1"]}).json()["id"]
    client.post(f"{API}/recordings/{rid}/roster", json={"effective_from": z(m0), "source": "head_coach", "substitutions_remaining": 3})
    plan = {"physiology": "expected", "physiology_interval_s": 1.0}
    for aid, st in (("a1", "on_field"), ("a2", "on_field"), ("b1", "bench")):
        client.post(f"{API}/recordings/{rid}/roster", json={"athlete_id": aid, "effective_from": z(m0), "status": st, **({"current_role": "centre back"} if st == "on_field" else {}),
                                                            "able_roles": ["centre back"], "source": "assistant_coach",
                                                            "inputs": plan if aid != "b1" else {"physiology": "not_used"}})
    return {"rid": rid, "m0": m0}


def run(client, frozen, rid, m0, until, a1_sends=lambda s: True):
    s0 = int((frozen["now"] - m0).total_seconds())
    for s in range(s0 + 1, until + 1):
        frozen["now"] = m0 + timedelta(seconds=s)
        if a1_sends(s):
            client.post(f"{API}/samples/physiological", json=hr("a1", frozen["now"]))
        client.post(f"{API}/samples/physiological", json=hr("a2", frozen["now"]))
        if s % 20 == 0:
            for aid in ("a1", "a2"):
                perf(client, rid, aid, frozen["now"] - timedelta(seconds=20), frozen["now"])


def team(client, rid):
    r = client.get(f"{API}/recordings/{rid}/team-review", params={"knowledge": "as_entered"}).json()
    return r, {x["athlete"]["id"]: x for x in r["athletes"]}


def test_fresh_stale_disconnected_fresh_and_a_healthy_neighbour(client, frozen, session):
    rid, m0 = session["rid"], session["m0"]
    gone = range(201, 251)
    run(client, frozen, rid, m0, 195, lambda s: s not in gone)
    _, by = team(client, rid)
    assert by["a1"]["outcome"] == "keep" and by["a1"]["evidence"]["sources"]["wearable"]["status"] == "present", by["a1"]["reason"]
    w = by["a1"]["evidence"]["sources"]["wearable"]["window"]
    assert w["expected"] == 20.0 and w["observed"] == 20 and w["coverage"] == 1.0 and w["expected_basis"].startswith("stated sampling interval")
    assert by["a1"]["evidence"]["used_by_review"] == {"physiological_index": "used", "performance_composite": "used", "tracking": "not an input of this rule version (shown for context only)"}
    seen = {}
    for until in (205, 215, 245, 280):
        run(client, frozen, rid, m0, until, lambda s: s not in gone)
        _, by = team(client, rid)
        seen[until] = (by["a1"]["evidence"]["sources"]["wearable"]["status"], by["a1"]["outcome"], by["a2"]["outcome"])
    assert seen[205] == ("present", "keep", "keep"), "5 s after the last reading: still within the index's staleness rule"
    assert seen[215][0] == "stale" and seen[215][1] == "insufficient_evidence" and seen[215][2] == "keep", seen
    assert seen[245][0] == "disconnected" and seen[245][1] == "insufficient_evidence" and seen[245][2] == "keep", seen
    assert seen[280] == ("present", "keep", "keep"), "30 s of fresh readings again: keep is affirmative again"
    _, by = team(client, rid)
    hist = [h["outcome"] for h in by["a1"]["review"]["history"]]
    assert "insufficient_evidence" in hist and hist[-1] == "keep", "the history keeps the gap; the current output is not an earlier keep"
    assert by["a1"]["evidence"]["summary"] == ["all planned inputs present"]
    assert by["b1"]["evidence"]["sources"]["wearable"]["status"] == "not_used" and by["b1"]["outcome"] == "insufficient_evidence", "bench: not on the field"
    assert by["u1"]["roster"]["status"] == "unknown" and by["u1"]["outcome"] == "insufficient_evidence" and "status unknown" in by["u1"]["reason"]
    assert by["u1"]["evidence"]["sources"]["wearable"]["status"] == "unknown"


def test_device_report_retries_replacements_and_rejections_are_told_apart(client, frozen, session):
    rid, m0 = session["rid"], session["m0"]
    run(client, frozen, rid, m0, 100)
    sample = hr("a2", frozen["now"])
    first = client.get(f"{API}/recordings/{rid}/evidence").json()
    frozen["now"] += timedelta(milliseconds=400)
    assert client.post(f"{API}/samples/physiological", json=sample).status_code == 200, "a retry of the same sample"
    assert client.post(f"{API}/samples/physiological", json={**hr("a2", m0 + timedelta(seconds=97)), "heart_rate_bpm": 150.0}).status_code == 200, "a replacement"
    assert client.post(f"{API}/samples/physiological", json={**sample, "heart_rate_bpm": -5}).status_code == 422
    assert client.post(f"{API}/samples/physiological", json={**sample, "timestamp": z(frozen["now"] + timedelta(hours=1))}).status_code == 422
    assert client.post(f"{API}/devices/status", json={"device_id": "strap-a1", "timestamp": z(frozen["now"]), "connected": False}).status_code in (200, 201)
    ev = client.get(f"{API}/recordings/{rid}/evidence").json()
    by = {x["athlete"]["id"]: x["evidence"]["sources"] for x in ev["athletes"]}
    w = by["a2"]["wearable"]
    assert w["window"]["retries"] == 1 and w["window"]["replacements"] == 1 and w["rejected_writes"]["count"] == 2
    assert w["last_received_at"] == first["athletes"][1]["evidence"]["sources"]["wearable"]["last_received_at"], "a retry keeps the first arrival's receipt"
    assert by["a1"]["wearable"]["status"] == "disconnected" and "reported connected = false" in by["a1"]["wearable"]["basis"]
    assert "recommendation" not in str(ev["note"]).replace("no recommendation", ""), "the evidence endpoint calculates no recommendation"


def test_no_profile_keeps_evidence_but_calculates_nothing(client, frozen, session, monkeypatch):
    rid, m0 = session["rid"], session["m0"]
    run(client, frozen, rid, m0, 60)
    monkeypatch.setattr(review_api, "get_settings", lambda: Settings())
    r, by = team(client, rid)
    assert r["counts"]["outcomes"]["insufficient_evidence"] == 4 and "no experimental substitution profile" in by["a1"]["reason"]
    assert by["a1"]["evidence"]["sources"]["wearable"]["status"] == "present" and "no experimental review" in by["a1"]["evidence"]["used_by_review"]["note"]


def test_a_stopped_recording_is_reviewed_at_its_stop_and_says_so(client, frozen, session):
    rid, m0 = session["rid"], session["m0"]
    run(client, frozen, rid, m0, 120)
    client.post(f"{API}/recordings/{rid}/stop")
    frozen["now"] += timedelta(minutes=30)
    r, by = team(client, rid)
    assert r["recording_status"] == "stopped" and r["at"] == z(m0 + timedelta(seconds=120))
    assert by["a2"]["outcome"] == "keep" and by["a2"]["evidence"]["sources"]["wearable"]["age_s"] < 1, "ages at the stop, not 30 min later"
    assert any("recording stopped" in m for m in by["a2"]["evidence"]["summary"])


def test_stream_status_delayed_and_late_arrivals():
    t = T0
    s = PhysiologicalSample.model_validate(hr("a1", t - timedelta(seconds=4)))
    kw = {"stale_after": 10, "disconnected_after": 30, "delayed_after": 2}
    st = stream_status("expected", (s, {"received_at": z(t)}), t, "as_entered", **kw)
    assert st["status"] == "delayed" and "arrived 4.0 s after" in st["basis"]
    st = stream_status("expected", (s, None), t, "as_entered", **kw)
    assert st["status"] == "present" and st["availability"].startswith("assumed: no receipt metadata"), "compatibility assumption is labelled, not verified"
    assert stream_status("expected", (s, {"received_at": z(t - timedelta(seconds=4))}), t, "as_entered", late_in_window=3, **kw)["status"] == "delayed"
    assert stream_status("expected", None, t, "as_entered", **kw)["status"] == "unavailable"
    assert stream_status(None, None, t, "as_entered", **kw)["status"] == "unknown"
