"""A paced FICTIONAL match session against a running engine (used by scripts/demo_launcher.py --session match and by
scripts/verify_operational_workflow.py).

This is a scripted replay of made-up data, NOT live camera or wearable capture. Every sample and entry is sent while the
session runs and is timestamped with the wall-clock time at which it is sent, so the engine's live rules (receipt time,
knowledge=as_entered) apply as they would live. `speed` compresses the script: one scenario second passes every
1/speed wall seconds; observation times stay real send times. The live profiles' windows are in wall seconds, so
at a high speed the phases are short and sustained states may not have time to form.

SETUP (before the session)
  - a stopped "FICTIONAL pre-match rest" recording is written into the empty database before the engine starts (rest
    readings for each strap wearer, ending just before the session); the session then creates one baseline per strap
    wearer from it and records an acceptance by "fictional analyst (simulated)";
  - the fictional athletes are registered, a recording is started, roster state, match constraints and coach context
    are entered.
PHASES (fractions of the scenario)
  0.00-0.30  every starter maintained (performance quality 0.75)
  0.20-0.45  p08's physiology elevated, then recovers
  0.30-      p02's performance declines (quality 0.2): independent of p02's physiology, which stays normal
  0.50       tactical change: formation 3-5-2, p05 asked to play left wing-back (p05 cannot; b03 can)
  0.55-      p06's strap stops sending (missing data)
  0.65       simulated coach response: p02's review is saved and deferred
  0.75       b01 becomes unavailable (a replacement option disappears)
  0.85       simulated lineup change: p05 off, b03 on as left wing-back
Physiology and performance come from separate seeded random streams. Performance observations are entered a few
seconds after each 20-scenario-second period ends, as an operator would.
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "scripts" / "examples"
LIVE_PROFILES = {"PHYSIOLOGICAL_INDEX_PROFILE_PATH": EXAMPLES / "review" / "fictional_live_index_profile.json",
                 "SUBSTITUTION_PROFILE_PATH": EXAMPLES / "review" / "fictional_live_substitution_profile.json",
                 "PERFORMANCE_RUBRIC_PATH": EXAMPLES / "performance" / "fictional_rubric_example.json"}
PERIOD_S = 20          # scenario seconds per performance observation period
REST_S = 240           # seconds of pre-match rest readings
# id, name, jersey, starter, current role, able roles, has a strap
PLAYERS = [
    ("p01", "Fictional Midfielder A", 8, True, "central midfielder", ["central midfielder"], True),
    ("p02", "Fictional Midfielder B", 6, True, "central midfielder", ["central midfielder", "defensive midfielder"], True),
    ("p03", "Fictional Centre Back A", 4, True, "centre back", ["centre back"], True),
    ("p04", "Fictional Centre Back B", 5, True, "centre back", ["centre back", "right back"], True),
    ("p05", "Fictional Left Back", 3, True, "left back", ["left back", "centre back"], True),
    ("p06", "Fictional Right Back", 2, True, "right back", ["right back"], True),
    ("p07", "Fictional Goalkeeper", 1, True, "goalkeeper", ["goalkeeper"], True),
    ("p08", "Fictional Winger", 7, True, "winger", ["winger", "forward"], True),
    ("b01", "Fictional Bench Midfielder", 14, False, None, ["central midfielder", "defensive midfielder", "centre back"], True),
    ("b02", "Fictional Bench Utility", 15, False, None, ["central midfielder", "right back"], False),
    ("b03", "Fictional Bench Wing-back", 16, False, None, ["left wing-back", "left midfielder"], False),
    ("b04", "Fictional Bench Forward", 17, False, None, ["forward", "winger"], False),
]
TEAM = "fictional-live-team"


def iso(t: datetime) -> str:
    return t.isoformat().replace("+00:00", "Z")


def strain(pid: str, f: float) -> float:
    """0 rest, 1 normal play, 2 elevated, as a function of the scenario fraction f (physiology only)."""
    if pid.startswith("b"):
        return 0.15
    if pid == "p08" and 0.20 <= f < 0.45:
        return 2.0
    return 1.0


def quality(pid: str, f: float) -> float:
    """Performance quality as a function of the scenario fraction (performance only; never reads physiology)."""
    return 0.2 if pid == "p02" and f >= 0.30 else 0.75


def physiological(pid: str, lv: float, base: dict, rng: random.Random, t: datetime) -> dict:
    hr = base["hr"] + 80 * min(lv, 1) + 25 * max(0, lv - 1) + rng.gauss(0, 3 if lv < 0.5 else 4)
    br = base["br"] + 20 * min(lv, 1) + 10 * max(0, lv - 1) + rng.gauss(0, 1.5)
    rm = max(3.0, base["rm"] - 38 * min(lv, 1) - 11 * max(0, lv - 1) + rng.gauss(0, 8 if lv < 0.5 else 3))
    return {"athlete_id": pid, "timestamp": iso(t), "source": "simulated", "device_id": f"fictional-strap-{pid}", "heart_rate_bpm": round(hr, 1),
            "breathing_rate_bpm": round(br, 1), "hrv_rmssd_ms": round(rm, 1), "signal_quality": 0.92}


def rest_bases(rng: random.Random) -> dict:
    return {pid: {"hr": 58 + rng.random() * 8, "br": 13 + rng.random() * 2, "rm": 60 + rng.random() * 10} for pid, *_ , strap in PLAYERS if strap}


def rest_package(end: datetime, bases: dict, rng: random.Random) -> bytes:
    """A stopped pre-match rest recording (current package format) ending at `end`, for the strap wearers."""
    from app.core.recording_package import build_package  # noqa: PLC0415
    from app.models import Athlete, PhysiologicalSample  # noqa: PLC0415
    start = end - timedelta(seconds=REST_S)
    athletes = [Athlete(id=pid, name=name, jersey_number=n, sport="soccer", team_id=TEAM) for pid, name, n, *_, strap in PLAYERS if strap]
    samples = {a.id: {"physiological": [PhysiologicalSample.model_validate(physiological(a.id, 0.0, bases[a.id], rng, start + timedelta(seconds=s))) for s in range(REST_S)],
                      "tracking": []} for a in athletes}
    rec = {"id": "rec-fictional-pre-match-rest", "name": "FICTIONAL pre-match rest (made-up data)", "status": "stopped", "started_at": iso(start), "stopped_at": iso(end)}
    return build_package(rec, athletes, samples, [], {"note": "fictional pre-match rest"}, end)


def prepare_database(db_path: Path, end: datetime, seed: str = "athletrix-live-physiology") -> tuple[str, dict]:
    """Write the rest recording into the (not yet served) database; returns its id and the per-athlete rest levels."""
    from app.core.recording_package import read_package  # noqa: PLC0415
    from app.db import Database  # noqa: PLC0415
    from app.store.recording_import import import_package  # noqa: PLC0415
    rng = random.Random(seed)
    bases = rest_bases(rng)
    db = Database(db_path)
    try:
        rid = import_package(db, read_package(rest_package(end, bases, rng)), end).recording_id
    finally:
        db.close()
    return rid, bases


@dataclass
class SessionStats:
    recording_id: str | None = None
    sent: dict = field(default_factory=lambda: {"physiological": 0, "performance": 0, "context": 0, "roster": 0, "responses": 0})
    failed: dict = field(default_factory=dict)
    events: list = field(default_factory=list)
    reviews: list = field(default_factory=list)       # periodic team-review summaries (as_entered)

    def fail(self, what: str, detail: str) -> None:
        self.failed[what] = self.failed.get(what, 0) + 1
        if self.failed[what] <= 3:
            self.events.append(f"FAILED {what}: {detail[:160]}")


class Session:
    def __init__(self, base_url: str, rest_recording_id: str, bases: dict, *, duration_s: float = 360, speed: float = 1.0, out: Callable[[str], None] = print,
                 client: httpx.Client | None = None, sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 now: Callable[[], datetime] = lambda: datetime.now(UTC), status_every_s: float = 30) -> None:
        self.base, self.rest_id, self.bases = base_url.rstrip("/"), rest_recording_id, bases
        self.duration, self.speed, self.out = float(duration_s), float(speed), out
        self.client = client or httpx.Client(timeout=10.0)
        self.sleep, self.clock, self.now = sleep, clock, now
        self.status_every = status_every_s
        self.stats = SessionStats()
        self.phys_rng, self.perf_rng = random.Random("athletrix-live-physiology-match"), random.Random("athletrix-live-performance")

    # -- small API helpers: every failure is counted, never retried silently --------------------------------------------
    def post(self, what: str, path: str, body: dict, ok=(200, 201)) -> dict | None:
        try:
            r = self.client.post(self.base + path, json=body)
        except httpx.HTTPError as exc:
            self.stats.fail(what, f"{type(exc).__name__}: {exc}")
            return None
        if r.status_code not in ok:
            self.stats.fail(what, f"HTTP {r.status_code} {r.text}")
            return None
        if what in self.stats.sent:
            self.stats.sent[what] += 1
        return r.json() if r.content else {}

    def setup(self) -> str:
        for pid, name, n, *_ in PLAYERS:
            self.post("athletes", "/api/v1/athletes", {"id": pid, "name": name, "jersey_number": n, "sport": "soccer", "team_id": TEAM}, ok=(201, 409))
        for pid, *_, strap in PLAYERS:
            if not strap:
                continue
            rest = self.client.get(f"{self.base}/api/v1/recordings/{self.rest_id}").json()
            b = self.post("baselines", f"/api/v1/athletes/{pid}/baselines", {
                "source_recording_id": self.rest_id, "ranges": [{"start": rest["started_at"], "end": rest["stopped_at"]}], "condition": "seated_rest",
                "condition_note": "fictional: seated before the session", "signals": ["heart_rate_bpm", "breathing_rate_bpm", "hrv_rmssd_ms"],
                "min_signal_quality": 0.6, "min_samples": 120, "label": "FICTIONAL pre-match rest", "created_by": "fictional analyst (simulated)"})
            if b:
                self.post("baselines", f"/api/v1/athletes/{pid}/baselines/{b['id']}/decision",
                          {"decision": "accepted", "decided_by": "fictional analyst (simulated)", "note": "FICTIONAL demo decision, not a validation"})
        rec = self.post("recording", "/api/v1/recordings", {"name": "FICTIONAL live session (paced script, not live capture)", "athlete_ids": [p[0] for p in PLAYERS]})
        if rec is None:
            raise RuntimeError("could not start the session recording: " + "; ".join(self.stats.events[-2:]))
        self.rid = self.stats.recording_id = rec["id"]
        t = self.now()
        self.post("roster", f"/api/v1/recordings/{self.rid}/roster", {"effective_from": iso(t), "source": "head_coach", "recorded_by": "fictional coach (simulated)", "substitutions_remaining": 3,
                                                                       "do_not_bring_on": [{"athlete_id": "b04", "reason": "fictional: kept for the next fixture"}]})
        for pid, _, _, starter, role, able, _ in PLAYERS:
            self.post("roster", f"/api/v1/recordings/{self.rid}/roster", {"athlete_id": pid, "effective_from": iso(t), "status": "on_field" if starter else "bench",
                                                                           **({"current_role": role} if starter else {}), "able_roles": able, "source": "assistant_coach"})
        self.post("context", f"/api/v1/recordings/{self.rid}/context", {"effective_from": iso(t), "formation": {"status": "recorded", "value": "4-3-3"},
                                                                         "tactical_instruction": {"status": "recorded", "value": "press high after losing the ball"},
                                                                         "match_label": "FICTIONAL live session", "source": "head_coach", "certainty": "confirmed"})
        self.post("context", f"/api/v1/recordings/{self.rid}/context", {"athlete_id": "p03", "effective_from": iso(t), "role": {"status": "recorded", "value": "centre back"},
                                                                         "evidence": {"status": "recorded", "items": [{"kind": "leadership", "task": "organising the back line at set pieces", "description": "fictional"}]},
                                                                         "partnerships": {"status": "recorded", "items": [{"partner_athlete_id": "p04", "note": "fictional: covers p04's runs"}]},
                                                                         "source": "head_coach", "certainty": "confirmed"})
        return self.rid

    def performance(self, start: datetime, end: datetime, f: float) -> None:
        for pid, _, _, starter, role, *_ in PLAYERS:
            if not starter:
                continue
            q = quality(pid, f)
            rng = self.perf_rng
            base = {"athlete_id": pid, "role": role, "period_start": iso(start), "period_end": iso(end), "assessment": "assessed", "annotator": "fictional analyst (simulated)"}
            n = rng.randint(3, 6)
            self.post("performance", f"/api/v1/recordings/{self.rid}/performance", {**base, "area": "execution", "kind": "pass", "attempts": n, "completions": sum(rng.random() < 0.5 + 0.4 * q for _ in range(n))})
            n = rng.randint(3, 6)
            self.post("performance", f"/api/v1/recordings/{self.rid}/performance", {**base, "area": "execution", "kind": "turnover", "possessions": n, "turnovers": sum(rng.random() < 0.4 * (1 - q) for _ in range(n))})
            for kind, area in (("decision", "decision_making"), ("positioning", "movement_positioning")):
                n = rng.randint(1, 3)
                r = [rng.random() for _ in range(n)]
                g, a = sum(x < q for x in r), sum(q <= x < q + (1 - q) / 2 for x in r)
                self.post("performance", f"/api/v1/recordings/{self.rid}/performance", {**base, "area": area, "kind": kind, "opportunities": n, "rated_good": g, "rated_acceptable": a,
                                                                                        "rated_poor": n - g - a, "criterion": "fictional criterion"})
            x = rng.random()
            self.post("performance", f"/api/v1/recordings/{self.rid}/performance", {**base, "area": "coach_strategy", "kind": "tactical_observation", "instruction": "press high after losing the ball",
                                                                                    "outcome": "followed" if x < q else ("partially_followed" if x < q + (1 - q) / 2 else "not_followed")})

    def event(self, f: float, done: set) -> None:
        t = iso(self.now())
        if f >= 0.50 and "tactic" not in done:
            done.add("tactic")
            self.post("context", f"/api/v1/recordings/{self.rid}/context", {"effective_from": t, "formation": {"status": "recorded", "value": "3-5-2"},
                                                                             "tactical_instruction": {"status": "recorded", "value": "wing-backs hold the width"}, "source": "head_coach", "certainty": "confirmed"})
            self.post("context", f"/api/v1/recordings/{self.rid}/context", {"athlete_id": "p05", "effective_from": t, "role": {"status": "recorded", "value": "left wing-back"}, "source": "head_coach", "certainty": "confirmed"})
            self.stats.events.append(f"{t} tactical change: 3-5-2, p05 asked to play left wing-back")
        if f >= 0.55 and "strap" not in done:
            done.add("strap")
            self.stats.events.append(f"{t} p06's strap stops sending (missing data from here on)")
        if f >= 0.65 and "defer" not in done:
            done.add("defer")
            rv = self.post("reviews", f"/api/v1/recordings/{self.rid}/athletes/p02/reviews", {"kind": "substitution", "at": t, "knowledge": "as_entered", "label": "FICTIONAL session"})
            if rv:
                self.post("responses", f"/api/v1/recordings/{self.rid}/responses", {"athlete_id": "p02", "kind": "defer", "client_token": f"session-defer-{self.rid[-6:]}", "saved_review_id": rv["id"],
                                                                                    "reason": "SIMULATED coach decision: one more phase", "simulated": True, "recorded_by": "demo script (simulated)"})
                self.stats.events.append(f"{t} SIMULATED response: p02's review ({rv['result']['outcome']}) saved and deferred")
        if f >= 0.75 and "b01" not in done:
            done.add("b01")
            self.post("roster", f"/api/v1/recordings/{self.rid}/roster", {"athlete_id": "b01", "effective_from": t, "status": "unavailable", "able_roles": PLAYERS[8][5],
                                                                           "source": "assistant_coach", "note": "fictional: no longer available (coach decision)"})
            self.stats.events.append(f"{t} b01 becomes unavailable (a replacement option disappears)")
        if f >= 0.85 and "lineup" not in done:
            done.add("lineup")
            r = self.post("responses", f"/api/v1/recordings/{self.rid}/responses", {"athlete_id": "p05", "kind": "lineup_change", "client_token": f"session-lineup-{self.rid[-6:]}", "simulated": True,
                                                                                    "recorded_by": "demo script (simulated)", "lineup": {"outgoing_athlete_id": "p05", "incoming_athlete_id": "b03",
                                                                                                                                          "effective_from": t, "incoming_role": "left wing-back"}})
            if r:
                self.stats.events.append(f"{t} SIMULATED lineup change: p05 off, b03 on as left wing-back ({len(r['roster_entry_ids'])} roster entries)")

    def status(self, f: float) -> None:
        try:
            r = self.client.get(f"{self.base}/api/v1/recordings/{self.rid}/team-review", params={"knowledge": "as_entered"}).json()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            self.stats.fail("team_review", str(exc))
            return
        summary = {x["athlete"]["id"]: x.get("outcome", "not reviewed") for x in r.get("athletes", [])}
        self.stats.reviews.append({"fraction": round(f, 3), "at": r.get("at"), "compute_ms": r.get("compute_ms"), "outcomes": summary})
        short = {"keep": "K", "consider_replacement": "C", "review_later": "R", "insufficient_evidence": "I"}
        self.out(f"  {f:4.0%} of the script · team review (as entered) in {r.get('compute_ms')} ms: " + " ".join(f"{a}:{short.get(o, '?')}" for a, o in summary.items()))

    def run(self) -> SessionStats:
        steps = int(self.duration)
        start = self.clock()
        period_start, done, last_status = self.now(), set(), -1e9
        for step in range(steps):
            wait = start + step / self.speed - self.clock()
            if wait > 0:
                self.sleep(wait)
            f = step / steps
            t = self.now()
            for pid, *_, strap in PLAYERS:
                if strap and not (pid == "p06" and f >= 0.55):
                    self.post("physiological", "/api/v1/samples/physiological", physiological(pid, strain(pid, f), self.bases[pid], self.phys_rng, t))
            if step and step % PERIOD_S == 0:
                self.performance(period_start, t, f)
                period_start = t
            self.event(f, done)
            if self.clock() - last_status >= self.status_every:
                last_status = self.clock()
                self.status(f)
        self.status(1.0)
        return self.stats
