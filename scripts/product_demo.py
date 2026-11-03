"""Reproducible FICTIONAL full-product demo: physiology vs baseline, four-area performance, coach context, roster state
and the experimental substitution review, end to end. Every athlete, value, profile and decision here is made up.

USAGE
    python scripts/product_demo.py run [--report FILE]      # everything in an isolated temporary database (default)
    python scripts/product_demo.py build --out DIR          # write the demo package and print how to load it in the dashboard
    python scripts/product_demo.py register --url URL       # add the fictional athletes to that engine's registry (for the dashboard's athlete list)

WHAT IT GENERATES (one 50-minute fictional match, recording package format 5)
  Physiology and performance are generated INDEPENDENTLY, from separate seeded random streams and separate scenario
  curves: physiology from a per-athlete "strain level" over time (rest, normal play, elevated), performance counts from
  a per-athlete "quality" curve. Performance is never derived from physiology or the other way round, so nothing here
  can be read as evidence that the two are related.
    - 1 Hz heart rate, breathing rate and HRV (RMSSD) from fictional straps for the starters and one bench player;
      a seated-rest baseline from the first 280 s for each strap wearer, accepted by a fictional analyst;
    - manual performance observations every 5 minutes in all four areas (passes, possessions, decisions, positioning,
      coach instructions), entered "after the fact";
    - coach context (formation, instruction, a role change, leadership and partnership evidence), roster state and match
      constraints.
  Scenarios (the athlete reviewed and the expected experimental output):
    S1 p01  elevated physiological index, performance maintained                -> review_later
    S2 p02  sustained performance decline, adequate physiology                  -> consider_replacement (bench options)
    S3 p03  the same decline, but recorded leadership and a partnership          -> review_later (coach weighs the context)
    S4 p05  formation change asks for a role the player cannot play              -> consider_replacement (role fit)
    S5 p06  strap lost after 20 minutes, no assessable performance evidence      -> insufficient_evidence
    S6 p07  goalkeeper decline, no goalkeeper on the bench                       -> review_later (no suitable replacement)
    S7 p08  elevated index from 12 to 24 minutes, then recovery                  -> review_later at 23:00, keep at 40:00
    S8 p09  camera-reviewed evidence with uncertain player identity              -> insufficient_evidence
    N1 p04  one noisy 60 s spike in physiology                                   -> keep (a single reading does not flip it)

`run` then checks: two hand-worked calculations done independently of the engine's code, saving and reproducing every
review, a restart, a performance correction, a configuration change, export and import into a second database, and
removal of an athlete. Exit code 0 when every check passes.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import shutil
import sqlite3
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.physiology_index import baseline_stats  # noqa: E402
from app.core.recording_package import build_package, read_package  # noqa: E402
from app.models import Athlete, PhysiologicalSample  # noqa: E402
from app.models.baseline import Baseline, BaselineCreate, RecordedDecision  # noqa: E402
from app.models.context import ContextEntry  # noqa: E402
from app.models.performance import PerformanceObservation  # noqa: E402
from app.models.roster import RosterEntry  # noqa: E402

EXAMPLES = ROOT / "scripts" / "examples"
PROFILE_FILES = {"PHYSIOLOGICAL_INDEX_PROFILE_PATH": EXAMPLES / "review" / "fictional_index_profile.json",
                 "SUBSTITUTION_PROFILE_PATH": EXAMPLES / "review" / "fictional_substitution_profile.json",
                 "PERFORMANCE_RUBRIC_PATH": EXAMPLES / "performance" / "fictional_rubric_example.json"}
START = datetime(2026, 11, 1, 14, 0, 0, tzinfo=UTC)
DURATION_S, MATCH_FROM_S, BASELINE_TO_S = 3000, 300, 280
REC = "rec-fictional-demo-match"
T_MAIN = 2400          # 40:00
T_EARLY = 1380         # 23:00

# id, name, jersey, on the field?, current role, able roles, has a strap
PLAYERS = [
    ("p01", "Fictional Midfielder A", 8, True, "central midfielder", ["central midfielder"], True),
    ("p02", "Fictional Midfielder B", 6, True, "central midfielder", ["central midfielder", "defensive midfielder"], True),
    ("p03", "Fictional Centre Back A", 4, True, "centre back", ["centre back"], True),
    ("p04", "Fictional Centre Back B", 5, True, "centre back", ["centre back", "right back"], True),
    ("p05", "Fictional Left Back", 3, True, "left back", ["left back", "centre back"], True),
    ("p06", "Fictional Right Back", 2, True, "right back", ["right back"], True),
    ("p07", "Fictional Goalkeeper", 1, True, "goalkeeper", ["goalkeeper"], True),
    ("p08", "Fictional Winger", 7, True, "winger", ["winger", "forward"], True),
    ("p09", "Fictional Forward", 9, True, "forward", ["forward"], True),
    ("b01", "Fictional Bench Midfielder", 14, False, None, ["central midfielder", "defensive midfielder", "centre back"], True),
    ("b02", "Fictional Bench Utility", 15, False, None, ["central midfielder", "right back"], False),
    ("b03", "Fictional Bench Wing-back", 16, False, None, ["left wing-back", "left midfielder"], False),
    ("b04", "Fictional Bench Forward", 17, False, None, ["forward", "winger"], False),
]
SCENARIOS = [("S1", "p01", T_MAIN, "review_later", "elevated physiological index, performance maintained"),
             ("S2", "p02", T_MAIN, "consider_replacement", "sustained performance decline with adequate physiological evidence"),
             ("S3", "p03", T_MAIN, "review_later", "decline, but recorded leadership and an on-field partnership"),
             ("S4", "p05", T_MAIN, "consider_replacement", "formation change asks for a role the player cannot play"),
             ("S5", "p06", T_MAIN, "insufficient_evidence", "missing and stale information"),
             ("S6", "p07", T_MAIN, "review_later", "no suitable replacement on the bench"),
             ("S7a", "p08", T_EARLY, "review_later", "elevated index (12-24 min), before recovery"),
             ("S7b", "p08", T_MAIN, "keep", "the same player after recovery"),
             ("S8", "p09", T_MAIN, "insufficient_evidence", "uncertain player identity in the camera-reviewed evidence"),
             ("N1", "p04", T_MAIN, "keep", "one noisy physiological reading does not flip the output")]


def at(s: float) -> datetime:
    return START + timedelta(seconds=s)


def z(s: float) -> str:
    return at(s).isoformat().replace("+00:00", "Z")


# ---- physiology (seeded stream "physiology"; never reads performance) ---------------------------------------------------

def strain(pid: str, s: int) -> float:
    """0 rest, 1 normal play, 2 elevated: the scenario's physiology curve."""
    if pid.startswith("b"):
        return 0.15
    if s < MATCH_FROM_S:
        return 0.0
    level = min(1.0, (s - MATCH_FROM_S) / 60)          # 60 s ramp into play
    if pid == "p01" and s >= 1440:
        level = 2.0
    if pid == "p08" and 720 <= s < 1440:
        level = 2.0
    if pid == "p04" and 1980 <= s < 2040:                # a single noisy minute (33:00-34:00)
        level = 2.0
    return level


def physiology(pid: str, rng: random.Random) -> list[PhysiologicalSample]:
    rest_hr, rest_br, rest_rmssd = 58 + rng.random() * 8, 13 + rng.random() * 2, 60 + rng.random() * 10
    out = []
    for s in range(DURATION_S):
        if pid == "p06" and s >= 1200:                   # the strap lost contact at 20:00 and never came back
            break
        lv = strain(pid, s)
        hr = rest_hr + 80 * min(lv, 1) + 25 * max(0, lv - 1) + rng.gauss(0, 3 if lv == 0 else 4)
        br = rest_br + 20 * min(lv, 1) + 10 * max(0, lv - 1) + rng.gauss(0, 1.5)
        rm = max(3.0, rest_rmssd - 38 * min(lv, 1) - 11 * max(0, lv - 1) + rng.gauss(0, 8 if lv == 0 else 3))
        q = 0.4 if rng.random() < 0.02 else 0.92
        out.append(PhysiologicalSample(athlete_id=pid, timestamp=at(s), source="wearable", device_id=f"fictional-strap-{pid}", heart_rate_bpm=round(hr, 1),
                                       breathing_rate_bpm=round(br, 1), hrv_rmssd_ms=round(rm, 1), signal_quality=q))
    return out


# ---- performance (seeded stream "performance"; never reads physiology) ------------------------------------------------

def quality(pid: str, s: int) -> float:
    if pid in ("p02", "p03", "p07") and s >= 1200:
        return 0.2
    return {"p01": 0.75, "p02": 0.75, "p03": 0.75, "p07": 0.7}.get(pid, 0.7)


def performance(pid: str, role: str, rng: random.Random) -> list[PerformanceObservation]:
    out = []

    def add(n, a, b, **kw):
        created = at(b + 60)
        out.append(PerformanceObservation(id=f"obs-demo-{pid}-{a:04d}-{n}", recording_id=REC, athlete_id=pid, role=role, period_start=at(a), period_end=at(b), created_at=created,
                                          annotator="fictional analyst", note="FICTIONAL demo observation", **kw))
    for a in range(MATCH_FROM_S, DURATION_S, 300):
        b = a + 300
        if pid == "p06":
            add("x", a, b, area="execution", kind="pass", assessment="unable_to_assess", unable_reason="off_camera")
            continue
        q = quality(pid, a)
        cam = pid == "p09"
        extra = {"evidence_source": "human_review_of_camera", "identity": "uncertain" if a >= 1500 else "confirmed",
                 "frames": [{"video": "fictional-match.mp4", "frame": int(a * 25)}]} if cam else {}
        n = rng.randint(6, 10)
        add("pass", a, b, area="execution", kind="pass", assessment="assessed", attempts=n, completions=sum(rng.random() < 0.5 + 0.4 * q for _ in range(n)), **extra)
        n = rng.randint(6, 10)
        add("poss", a, b, area="execution", kind="turnover", assessment="assessed", possessions=n, turnovers=sum(rng.random() < 0.4 * (1 - q) for _ in range(n)), **extra)
        for kind, area, crit in (("decision", "decision_making", "chose the option that broke the press"), ("positioning", "movement_positioning", "kept the agreed line and distances")):
            n = rng.randint(3, 5)
            r = [rng.random() for _ in range(n)]
            g, ac = sum(x < q for x in r), sum(q <= x < q + (1 - q) / 2 for x in r)
            add(kind, a, b, area=area, kind=kind, assessment="assessed", opportunities=n, rated_good=g, rated_acceptable=ac, rated_poor=n - g - ac, criterion=crit, **extra)
        x = rng.random()
        add("tac", a, b, area="coach_strategy", kind="tactical_observation", assessment="assessed", instruction="press high after losing the ball",
            outcome="followed" if x < q else ("partially_followed" if x < q + (1 - q) / 2 else "not_followed"), coach_context_id="ctx-demo-team-1", **extra)
    return out


# ---- context, roster, baselines ------------------------------------------------------------------------------------------

def context() -> list[ContextEntry]:
    def entry(i, s, **kw):
        return ContextEntry(id=i, recording_id=REC, created_at=at(s + 5), effective_from=at(s), source="head_coach", certainty="confirmed", recorded_by="fictional coach", **kw)
    return [entry("ctx-demo-team-1", MATCH_FROM_S, match_label="FICTIONAL demo match", formation={"status": "recorded", "value": "4-3-3"},
                  tactical_instruction={"status": "recorded", "value": "press high after losing the ball"}),
            entry("ctx-demo-p03", MATCH_FROM_S, athlete_id="p03", role={"status": "recorded", "value": "centre back"},
                  evidence={"status": "recorded", "items": [{"kind": "leadership", "task": "organising the back line at set pieces", "description": "fictional: calls the line and the marking at every corner", "observed_at": z(900)}]},
                  partnerships={"status": "recorded", "items": [{"partner_athlete_id": "p04", "note": "fictional: covers p04's runs into the left channel"}]}),
            entry("ctx-demo-team-2", 1800, formation={"status": "recorded", "value": "3-5-2"}, tactical_instruction={"status": "recorded", "value": "wing-backs hold the width"}),
            entry("ctx-demo-p05", 1800, athlete_id="p05", role={"status": "recorded", "value": "left wing-back"})]


def roster() -> list[RosterEntry]:
    out = [RosterEntry(id="ros-demo-team", recording_id=REC, created_at=at(MATCH_FROM_S + 5), effective_from=at(MATCH_FROM_S), source="head_coach", substitutions_remaining=3,
                       do_not_bring_on=[{"athlete_id": "b04", "reason": "fictional: coach is keeping b04 for the next fixture"}])]
    for pid, _, _, starter, role, able, _ in PLAYERS:
        out.append(RosterEntry(id=f"ros-demo-{pid}-0", recording_id=REC, created_at=at(5), effective_from=at(0), source="assistant_coach", athlete_id=pid, status="bench", able_roles=able))
        if starter:
            out.append(RosterEntry(id=f"ros-demo-{pid}-1", recording_id=REC, created_at=at(MATCH_FROM_S + 5), effective_from=at(MATCH_FROM_S), source="assistant_coach", athlete_id=pid,
                                   status="on_field", current_role=role, able_roles=able))
    return out


def baseline(pid: str, samples: list) -> Baseline:
    body = BaselineCreate(source_recording_id=REC, ranges=[{"start": z(0), "end": z(BASELINE_TO_S)}], condition="seated_rest", condition_note="fictional: seated on the bench before the warm-up",
                          signals=["heart_rate_bpm", "breathing_rate_bpm", "hrv_rmssd_ms"], min_signal_quality=0.6, required_source="wearable", min_samples=120,
                          label="FICTIONAL pre-match rest", created_by="fictional analyst")
    created = at(BASELINE_TO_S + 10)
    return Baseline(**body.model_dump(), id=f"bl-demo-{pid}", athlete_id=pid, version=1, created_at=created, data_start=at(0), data_end=at(BASELINE_TO_S),
                    stats=baseline_stats(body, samples), status="accepted",
                    decisions=[RecordedDecision(decision="accepted", decided_by="fictional analyst", note="FICTIONAL demo decision: rest period looked steady", decided_at=created)])


def build() -> bytes:
    """The demo package (format 5). Deterministic: same bytes apart from generated_at, which is fixed too."""
    phys_rng, perf_rng = random.Random("athletrix-demo-physiology"), random.Random("athletrix-demo-performance")
    athletes = [Athlete(id=pid, name=name, jersey_number=n, sport="soccer", team_id="fictional-team") for pid, name, n, *_ in PLAYERS]
    samples, perf, baselines = {}, [], []
    for pid, _, _, starter, role, _, strap in PLAYERS:
        p = physiology(pid, phys_rng) if strap else []
        samples[pid] = {"physiological": p, "tracking": []}
        if strap:
            baselines.append(baseline(pid, p))
        if starter:
            perf += performance(pid, role, perf_rng)
    rec = {"id": REC, "name": "FICTIONAL demo match (made-up data)", "status": "stopped", "started_at": z(0), "stopped_at": z(DURATION_S)}
    return build_package(rec, athletes, samples, [], {"note": "fictional demo"}, at(DURATION_S + 600), None, None, context(), perf, baselines, roster(), [])


# ---- independent hand-worked checks (plain arithmetic; none of the engine's calculation code) ------------------------------

def hand_index(pkg, pid: str, t: int, profile: dict) -> float:
    rows = [s for s in pkg.samples[pid]["physiological"]]
    base = [s for s in rows if s.timestamp <= at(BASELINE_TO_S) and s.signal_quality >= 0.6]
    win = [s for s in rows if at(t - profile["window_seconds"]) < s.timestamp <= at(t) and s.signal_quality >= 0.6]
    total, wsum = 0.0, 0.0
    for spec in profile["signals"]:
        f = spec["signal"]
        bv = [getattr(s, f) for s in base]
        mu = sum(bv) / len(bv)
        sd = math.sqrt(sum((v - mu) ** 2 for v in bv) / (len(bv) - 1))
        m = sum(getattr(s, f) for s in win) / len(win)
        x = (m - mu) / sd if spec["transform"] == "z_score" else 100 * (m - mu) / mu
        if spec["direction"] == "lower_is_more_strain":
            x = -x
        score = 100 * min(1, max(0, (x - spec["zero_at"]) / (spec["hundred_at"] - spec["zero_at"])))
        total += spec["weight"] * score
        wsum += spec["weight"]
    return round(total / wsum, 2)


def hand_composite(pkg, pid: str, t: int, window: int, rubric: dict) -> float:
    obs = [o for o in pkg.performance if o.athlete_id == pid and o.period_end <= at(t) and o.period_start >= at(t - window) and o.assessment == "assessed"]
    def band(rate, m):
        return 100 * min(1, max(0, (rate - m["floor"]) / (m["ceiling"] - m["floor"])))
    a = rubric["areas"]
    pc = sum(o.completions for o in obs if o.kind == "pass") / sum(o.attempts for o in obs if o.kind == "pass")
    pr = 1 - sum(o.turnovers for o in obs if o.kind == "turnover") / sum(o.possessions for o in obs if o.kind == "turnover")
    m1, m2 = a["execution"]["measures"]
    execution = (band(pc, m1) * m1["weight"] + band(pr, m2) * m2["weight"]) / (m1["weight"] + m2["weight"])
    scores = {"execution": execution}
    for area, kind in (("decision_making", "decision"), ("movement_positioning", "positioning")):
        m = a[area]["measures"][0]
        sel = [o for o in obs if o.kind == kind]
        scores[area] = band((sum(o.rated_good for o in sel) + m["partial_credit"] * sum(o.rated_acceptable for o in sel)) / sum(o.opportunities for o in sel), m)
    m = a["coach_strategy"]["measures"][0]
    tac = [o for o in obs if o.kind == "tactical_observation"]
    scores["coach_strategy"] = band((sum(o.outcome == "followed" for o in tac) + m["partial_credit"] * sum(o.outcome == "partially_followed" for o in tac)) / len(tac), m)
    w = {k: a[k]["weight"] for k in scores}
    return round(sum(round(scores[k], 2) * w[k] for k in scores) / sum(w.values()), 2)


# ---- run --------------------------------------------------------------------------------------------------------------------

class Engine:
    """An in-process engine on one database file with the given profile files (isolated from any development data)."""

    def __init__(self, path: Path, profiles: dict[str, Path]) -> None:
        from fastapi.testclient import TestClient  # noqa: PLC0415

        from app.core.config import get_settings  # noqa: PLC0415
        from app.db import Database, set_database  # noqa: PLC0415
        from app.main import create_app  # noqa: PLC0415
        for k, v in profiles.items():
            os.environ[k] = str(v)
        get_settings.cache_clear()
        self.db = Database(path)
        set_database(self.db)
        self.client = TestClient(create_app())

    def get(self, url, **params):
        r = self.client.get("/api/v1" + url, params=params)
        assert r.status_code == 200, (url, r.status_code, r.text[:300])
        return r.json()

    def post(self, url, body):
        r = self.client.post("/api/v1" + url, json=body)
        assert r.status_code in (200, 201), (url, r.status_code, r.text[:300])
        return r.json()

    def close(self):
        from app.db import set_database  # noqa: PLC0415
        self.db.close()
        set_database(None)


def run(report_path: Path | None, out=print) -> int:
    from app.store.recording_import import import_package  # noqa: PLC0415
    checks, results = [], {}

    def check(name, ok, detail=""):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        out(f"  [{'ok' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))

    work = Path(tempfile.mkdtemp(prefix="athletrix-demo-"))
    saved_env = {k: os.environ.get(k) for k in (*PROFILE_FILES, "DATABASE_PATH")}
    try:
        data = build()
        pkg = read_package(data)
        out(f"FICTIONAL DEMO: {len(pkg.athletes)} made-up athletes, {pkg.summary()['counts']['physiological_samples']} physiological samples, "
            f"{len(pkg.performance)} performance observations, {len(pkg.baselines)} baselines; isolated database in {work}")
        from app.db import Database  # noqa: PLC0415
        db = Database(work / "demo.db")
        rid = import_package(db, pkg, at(DURATION_S + 3600)).recording_id
        db.close()
        eng = Engine(work / "demo.db", PROFILE_FILES)
        out("\nSCENARIOS (experimental outputs; the coach decides)")
        for sid, pid, t, expected, what in SCENARIOS:
            r = eng.get(f"/recordings/{rid}/athletes/{pid}/substitution-review", at=z(t))
            results[sid] = r
            reps = ", ".join(f"{c['athlete_id']} ({c['role_fit']}{'; physiology unavailable' if not c['physiology']['available'] else ''})" for c in r["replacements"] if c["role_fit"] == "fits") or "none"
            check(f"{sid} {pid} at {z(t)[11:16]}: {what}", r["outcome"] == expected, f"{r['outcome']} (expected {expected}); index {r['player']['physiology']['value']}, "
                  f"performance {r['player']['performance']['value']}; triggers {r['triggers'] or 'none'}; replacements {reps}")
            for line in r["reasons"] + [f"tradeoff: {x}" for x in r["tradeoffs"]] + [f"constraint: {x}" for x in r["constraints_applied"]] + [f"missing: {x}" for x in r["missing"][:2]]:
                out(f"        - {line[:160]}")
        n1 = [h["physiology_state"] for h in results["N1"]["history"]]
        check("N1 history shows the single high reading as unconfirmed, not a concern", "elevated_unconfirmed" in n1 and "concern" not in n1, f"states seen: {sorted(set(n1))}")
        s2 = {c["athlete_id"]: c for c in results["S2"]["replacements"]}
        check("S2 lists a bench option without physiology as unavailable, not fresh", s2["b02"]["role_fit"] == "fits" and not s2["b02"]["physiology"]["available"]
              and "not evidence that this athlete is fresh" in s2["b02"]["physiology"]["note"])
        check("S2 never offers the athlete the coach ruled out", "b04" not in {c["athlete_id"] for c in results["S2"]["replacements"] if c["role_fit"] == "fits" and not c["blocked_reason"]})

        out("\nHAND-WORKED CHECKS (independent arithmetic, not the engine's code)")
        prof = json.loads(PROFILE_FILES["PHYSIOLOGICAL_INDEX_PROFILE_PATH"].read_text())
        rub = json.loads(PROFILE_FILES["PERFORMANCE_RUBRIC_PATH"].read_text())
        hi, api = hand_index(pkg, "p01", T_MAIN, prof), eng.get(f"/recordings/{rid}/athletes/p01/physiological-index", at=z(T_MAIN))["value"]
        check("p01 physiological index at 40:00", abs(hi - api) < 0.02, f"hand {hi}, engine {api}")
        hc = hand_composite(pkg, "p02", T_MAIN, 900, rub)
        api = eng.get(f"/recordings/{rid}/athletes/p02/performance-review", at=z(T_MAIN), window_s=900)["composite"]["value"]
        check("p02 performance composite over 25:00-40:00", abs(hc - api) < 0.02, f"hand {hc}, engine {api}")

        out("\nPERSISTENCE, CORRECTIONS, CONFIGURATION, EXPORT/IMPORT, REMOVAL")
        saved = {sid: eng.post(f"/recordings/{rid}/athletes/{pid}/reviews", {"kind": "substitution", "at": z(t), "label": f"FICTIONAL demo {sid}"}) for sid, pid, t, *_ in SCENARIOS}
        rep = {sid: eng.get(f"/recordings/{rid}/reviews/{s['id']}/reproduce") for sid, s in saved.items()}
        check("every saved review reproduces from its stored configuration and evidence", all(r["matches"] for r in rep.values()), f"{sum(r['matches'] for r in rep.values())}/{len(rep)}")
        eng.close()
        eng = Engine(work / "demo.db", PROFILE_FILES)
        again = eng.get(f"/recordings/{rid}/athletes/p02/substitution-review", at=z(T_MAIN))
        check("after a restart the same review gives the same output", again == results["S2"])
        before = eng.get(f"/recordings/{rid}/athletes/p02/performance-review", at=z(T_MAIN), window_s=900)
        target = next(o for o in before["observations"] if o["kind"] == "pass" and o["status"] == "counted")
        eng.post(f"/recordings/{rid}/performance", {**{k: target[k] for k in ("athlete_id", "role", "area", "kind", "assessment", "attempts", "period_start", "period_end")},
                                                     "completions": target["attempts"], "supersedes": target["id"], "correction_reason": "FICTIONAL: recounted from the video"})
        after = eng.get(f"/recordings/{rid}/athletes/p02/performance-review", at=z(T_MAIN), window_s=900)
        check("a correction replaces the earlier observation instead of adding to it", after["areas"]["execution"]["evidence"]["passes"]["attempts"] == before["areas"]["execution"]["evidence"]["passes"]["attempts"]
              and after["areas"]["execution"]["evidence"]["passes"]["completions"] > before["areas"]["execution"]["evidence"]["passes"]["completions"])
        r = eng.get(f"/recordings/{rid}/reviews/{saved['S2']['id']}/reproduce")
        check("the saved review still reproduces from its own evidence copy after the correction", r["matches"])
        changed = json.loads(PROFILE_FILES["SUBSTITUTION_PROFILE_PATH"].read_text())
        changed["version"], changed["performance"]["concern_below"], changed["performance"]["clear_at"] = "fictional-substitution-1-changed", 10, 20
        alt = work / "changed_profile.json"
        alt.write_text(json.dumps(changed))
        eng.close()
        eng = Engine(work / "demo.db", {**PROFILE_FILES, "SUBSTITUTION_PROFILE_PATH": alt})
        live = eng.get(f"/recordings/{rid}/athletes/p02/substitution-review", at=z(T_MAIN))
        r = eng.get(f"/recordings/{rid}/reviews/{saved['S2']['id']}/reproduce")
        check("a changed profile changes live results but not saved ones", live["outcome"] != results["S2"]["outcome"] and r["matches"] and r["configuration_differs_from_current"]["substitution"],
              f"live now {live['outcome']}; saved still {r['result']['outcome']}")
        pkg_bytes = eng.client.get(f"/api/v1/recordings/{rid}/package.zip").content
        eng.close()
        copy = read_package(pkg_bytes)
        check("the exported package (format 5) carries baselines, roster, saved reviews and the correction",
              copy.manifest["format_version"] == 5 and len(copy.baselines) == len(pkg.baselines) and len(copy.roster) == len(pkg.roster) and len(copy.reviews) == len(SCENARIOS)
              and len(copy.performance) == len(pkg.performance) + 1)
        db2 = Database(work / "second.db")
        rid2 = import_package(db2, copy, at(DURATION_S + 7200)).recording_id
        db2.close()
        eng = Engine(work / "second.db", PROFILE_FILES)
        imported = eng.get(f"/recordings/{rid2}/reviews")
        rep2 = [eng.get(f"/recordings/{rid2}/reviews/{x['id']}/reproduce") for x in imported]
        check("saved reviews reproduce on the second engine after import", len(rep2) == len(SCENARIOS) and all(x["matches"] and not x["recorded_samples_changed_for"] for x in rep2))
        eng.close()
        shutil.copy(work / "second.db", work / "removal.db")
        import importlib.util  # noqa: PLC0415
        spec = importlib.util.spec_from_file_location("local_data_demo", ROOT / "scripts" / "local_data.py")
        ld = importlib.util.module_from_spec(spec); spec.loader.exec_module(ld)
        lines = []
        code = ld.main(["remove-athlete", "b01", "--database", str(work / "removal.db"), "--execute", "--confirm", "b01"], out=lines.append)
        con = sqlite3.connect(work / "removal.db")
        left = {t: con.execute(f"SELECT COUNT(*) FROM {t} WHERE athlete_id = 'b01'").fetchone()[0] for t in ("athlete_baselines", "recording_roster", "recording_physiological_samples")}
        subs = con.execute("SELECT COUNT(*) FROM recording_reviews WHERE kind = 'substitution'").fetchone()[0]
        con.close()
        check("removing an athlete removes their baselines, roster state, samples and the saved reviews that embed their evidence",
              code == 0 and not any(left.values()) and subs == 0, f"left {left}, saved substitution reviews {subs}")
        eng = Engine(work / "removal.db", PROFILE_FILES)
        r = eng.get(f"/recordings/{rid2}/athletes/p02/substitution-review", at=z(T_MAIN))
        check("after removal b01 is no longer offered", "b01" not in {c["athlete_id"] for c in r["replacements"]}, r["outcome"])
        eng.close()
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        from app.core.config import get_settings  # noqa: PLC0415
        get_settings.cache_clear()
        shutil.rmtree(work, ignore_errors=True)
    failed = [c for c in checks if not c["ok"]]
    out(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed. FICTIONAL data and profiles: this demonstrates the software, not any real-world capability.")
    if report_path:
        report_path.write_text(json.dumps({"fictional": True, "checks": checks, "scenarios": {k: {"outcome": v["outcome"], "reasons": v["reasons"], "tradeoffs": v["tradeoffs"],
                                                                                              "missing": v["missing"], "triggers": v["triggers"]} for k, v in results.items()}}, indent=2))
    return 0 if not failed else 1


def main(argv=None, out=print) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="run every scenario and check in an isolated temporary database")
    r.add_argument("--report", type=Path, help="also write a JSON report here (must not exist)")
    b = sub.add_parser("build", help="write the demo package for loading into a dashboard engine")
    b.add_argument("--out", type=Path, required=True, help="directory to create (must not exist)")
    g = sub.add_parser("register", help="register the fictional athletes with a running demo engine (imports never touch the registry)")
    g.add_argument("--url", required=True, help="the demo engine, e.g. http://127.0.0.1:8010")
    args = ap.parse_args(argv)
    if args.command == "register":
        import httpx  # noqa: PLC0415
        for pid, name, n, *_ in PLAYERS:
            r = httpx.post(args.url.rstrip("/") + "/api/v1/athletes", json={"id": pid, "name": name, "jersey_number": n, "sport": "soccer", "team_id": "fictional-team"})
            out(f"{pid}: HTTP {r.status_code}" + ("" if r.status_code == 201 else f" {r.text[:120]}"))
        return 0
    if args.command == "run":
        if args.report and args.report.exists():
            out(f"{args.report} exists; not overwritten")
            return 2
        return run(args.report, out)
    if args.out.exists():
        out(f"{args.out} exists; not overwritten")
        return 2
    args.out.mkdir(parents=True)
    pkg = args.out / "fictional-demo-match.zip"
    pkg.write_bytes(build())
    out(f"wrote {pkg} (FICTIONAL data, recording package format 5)")
    out("load it into a separate demo database and start an engine with the fictional profiles (from this folder):")
    out(f"  python scripts/recording_package.py import {pkg} --database {args.out}/demo.db")
    env = " ".join(f"{k}={v.relative_to(ROOT)}" for k, v in PROFILE_FILES.items())
    out(f"  DATABASE_PATH={args.out}/demo.db {env} uvicorn app.main:app --port 8010")
    out("  python scripts/product_demo.py register --url http://127.0.0.1:8010      # the fictional athletes, for the dashboard's athlete list")
    out("then open the dashboard in engine mode against that port (allow its origin with CORS_ORIGINS), open the recording with Review and read the Experimental review card")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
