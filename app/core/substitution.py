"""EXPERIMENTAL substitution review: a small deterministic rule system over independent evidence (pure functions).

Outputs at a review time T for one athlete: keep | consider_replacement | review_later | insufficient_evidence, with the
reasons, the possible replacements, the missing information, the tradeoffs and a suggested next review. The coach makes
the decision; this is decision support, not an optimal-substitution claim, and nothing here is a trained model.

INPUTS, each kept separate and never derived from another:
  physiology   the experimental physiological index (app/core/physiology_index.py) at review points T, T - step, ...
               back to T - lookback (never before the recording started), each using only data available at that point
  performance  the four-area performance composite (app/core/performance.py) over the trailing performance window at
               each review point; never calculated from physiology
  context      the coach-context entries in force at T (role asked of the athlete; leadership, pressure-situation and
               partnership evidence) and the roster state and match constraints in force (app/models/roster.py)
No profile (SUBSTITUTION_PROFILE_PATH) means insufficient_evidence with nothing else calculated.

SUSTAINED STATES (hysteresis, so one noisy reading does not flip the output). Over the review points, oldest first:
  physiology   concern after `persistence_points` consecutive indexes >= concern_at; cleared after `clear_points`
               consecutive indexes < clear_below. A single high reading is "elevated_unconfirmed".
  performance  concern after `persistence_points` consecutive composites < concern_below; cleared after `clear_points`
               consecutive composites >= clear_at ("below_unconfirmed" before that).
  An unavailable point resets the running counts, and the state AT T is "unavailable" when T's own value is unavailable.

DECISION AT EACH POINT, in this order:
  1. the roster state does not show the athlete on the field                          -> insufficient_evidence
  2. triggers: performance concern; physiology concern; role mismatch (the role asked of the athlete, from coach context
     or else the roster's current role, is not among the roles the coach says the athlete can play; exact text)
  3. performance unavailable because evidence was left out for uncertain identity, and no role mismatch
                                                                                       -> insufficient_evidence
  4. no trigger: both histories unavailable -> insufficient_evidence; otherwise -> keep (missing evidence listed)
  5. physiology concern without performance concern or role mismatch                    -> review_later (an elevated
     index alone never recommends a replacement)
  6. performance concern only, physiology unavailable and the profile requires it       -> review_later
  7. replacement step (performance concern or role mismatch). Candidates: athletes on the bench at T, not named in
     do_not_bring_on, whose able roles include the required role. Missing bench physiology is reported as unavailable,
     never as freshness. Then, in order: the athlete is in keep_on_field -> keep (coach constraint); no substitutions
     remaining -> keep; no role-compatible candidate -> review_later; before no_substitution_before -> review_later at
     that time; recorded leadership / pressure-situation evidence or an on-field partnership with policy hold_for_review
     -> review_later (the coach weighs it; no bonus or score is applied); otherwise -> consider_replacement.
COOLDOWN. Walking the points oldest first, a change between keep and consider_replacement within cooldown_seconds of the
previous change is held back (the earlier output is kept and the hold is reported). insufficient_evidence and
review_later are never held back.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.core.performance import review as performance_review
from app.core.physiology_index import compute_index, iso, select_baseline
from app.models import Athlete
from app.models.baseline import Baseline, IndexProfile
from app.models.context import ContextEntry
from app.models.performance import PerformanceObservation, PerformanceRubric
from app.models.roster import RosterEntry
from app.models.substitution import SubstitutionProfile
from app.store.context import select_effective
from app.store.roster import effective_roster

LABEL = "Experimental substitution review: decision support from configured rules. The coach decides; not an optimal-substitution claim."
OUTCOMES = ("keep", "consider_replacement", "review_later", "insufficient_evidence")


@dataclass
class AthleteData:
    athlete: Athlete
    samples: list[tuple] = field(default_factory=list)          # (PhysiologicalSample, receipt dict or None), oldest first
    baselines: list[Baseline] = field(default_factory=list)
    observations: list[PerformanceObservation] = field(default_factory=list)


@dataclass
class ReviewInputs:
    recording_id: str
    started_at: datetime
    stopped_at: datetime | None
    athlete_id: str
    at: datetime
    athletes: dict[str, AthleteData]
    context: list[ContextEntry]
    roster: list[RosterEntry]
    substitution: tuple[SubstitutionProfile, str] | None
    index: tuple[IndexProfile, str] | None
    rubric: tuple[PerformanceRubric, str] | None


def norm(role: str | None) -> str | None:
    return role.strip().casefold() if role else None


def physiology_at(inp: ReviewInputs, athlete_id: str, t: datetime) -> dict:
    if inp.index is None:
        return {"available": False, "value": None, "reasons": ["no physiological index profile is configured (PHYSIOLOGICAL_INDEX_PROFILE_PATH)"]}
    profile, sha = inp.index
    d = inp.athletes[athlete_id]
    b, timing, why = select_baseline(d.baselines, profile, t)
    return compute_index(profile, sha, b, timing, d.samples, t, why)


def performance_at(inp: ReviewInputs, prof: SubstitutionProfile, athlete_id: str, t: datetime) -> dict:
    rubric, sha = inp.rubric if inp.rubric else (None, None)
    r = performance_review(inp.athletes[athlete_id].observations, rubric, sha, t, prof.performance.window_seconds)
    excluded_identity = sum(1 for o in r["observations"] if o["status"] == "identity_uncertain")
    sources: dict[str, int] = {}
    for a in r["areas"].values():
        for k, v in a["evidence"]["sources"].items():
            sources[k] = sources.get(k, 0) + v
    return {"available": r["composite"]["available"], "value": r["composite"]["value"], "reasons": r["composite"]["reasons"],
            "areas": {a: v["score"] for a, v in r["areas"].items()}, "counted_observation_ids": r["counted_observation_ids"],
            "identity_uncertain_excluded": excluded_identity, "fictional_rubric": r["composite"]["fictional_rubric"], "sources": sources}


def track(points: list[tuple[datetime, float | None]], high, low, persist: int, clear_n: int, labels: tuple[str, str]) -> list[str]:
    """Sustained state per point: unavailable | normal | <unconfirmed label> | concern (see the module docstring)."""
    latent, run_c, run_n, out = None, 0, 0, []
    for _, v in points:
        if v is None:
            run_c = run_n = 0
            out.append("unavailable")
            continue
        if latent != "concern":
            run_c = run_c + 1 if high(v) else 0
            if run_c >= persist:
                latent, run_n = "concern", 0
            else:
                latent = "normal"
            out.append(latent if latent == "concern" or run_c == 0 else labels[0])
        else:
            run_n = run_n + 1 if low(v) else 0
            if run_n >= clear_n:
                latent, run_c = "normal", 0
            out.append(latent)
    return out


def review_points(inp: ReviewInputs, prof: SubstitutionProfile) -> list[datetime]:
    pts, t = [], inp.at
    earliest = max(inp.started_at, inp.at - timedelta(seconds=prof.lookback_seconds))
    while t >= earliest:
        pts.append(t)
        t -= timedelta(seconds=prof.step_seconds)
    return pts[::-1]


def _context_at(inp: ReviewInputs, athlete_id: str, t: datetime) -> tuple[ContextEntry | None, ContextEntry | None]:
    team = select_effective([e for e in inp.context if e.athlete_id is None], t)
    mine = select_effective([e for e in inp.context if e.athlete_id == athlete_id], t)
    return team, mine


def _candidates(inp: ReviewInputs, t: datetime, required: str | None, team_roster: RosterEntry | None, roster: dict[str, RosterEntry]) -> list[dict]:
    blocked = {n.athlete_id: n.reason for n in (team_roster.do_not_bring_on if team_roster else [])}
    out = []
    for aid, e in sorted(roster.items()):
        if aid == inp.athlete_id or e.status != "bench" or aid not in inp.athletes:
            continue
        able = [norm(r) for r in e.able_roles]
        fit = "unknown" if required is None or not able else ("fits" if norm(required) in able else "does_not_fit")
        out.append({"athlete_id": aid, "name": inp.athletes[aid].athlete.name, "able_roles": e.able_roles, "role_fit": fit,
                    "blocked_reason": blocked.get(aid)})
    return out


def decide(inp: ReviewInputs, prof: SubstitutionProfile, t: datetime, phys_state: str, perf_state: str, phys: dict, perf: dict) -> dict:
    team_roster, roster = effective_roster(inp.roster, t)
    me = roster.get(inp.athlete_id)
    team_ctx, my_ctx = _context_at(inp, inp.athlete_id, t)
    reasons, missing, tradeoffs, constraints, triggers = [], [], [], [], []
    res = {"reasons": reasons, "missing": missing, "tradeoffs": tradeoffs, "constraints_applied": constraints, "triggers": triggers,
           "review_again_at": None, "replacements": []}

    def out(outcome, again=None):
        res["outcome"] = outcome
        if outcome == "review_later":
            res["review_again_at"] = iso(again or t + timedelta(seconds=prof.review_again_after_seconds))
        return res

    if me is None or me.status != "on_field":
        reasons.append("the roster state at this time does not show the athlete on the field" + (f" (status: {me.status})" if me else " (no roster entry)"))
        return out("insufficient_evidence")
    role_src = "coach_context" if my_ctx and my_ctx.role.status == "recorded" else ("roster" if me.current_role else None)
    required = my_ctx.role.value if role_src == "coach_context" else me.current_role
    able = [norm(r) for r in me.able_roles]
    role_mismatch = bool(required and able and norm(required) not in able)
    res["role"] = {"required": required, "required_from": role_src, "able_roles": me.able_roles,
                   "fit": None if not (required and able) else not role_mismatch,
                   "team_instruction": team_ctx.tactical_instruction.value if team_ctx and team_ctx.tactical_instruction.status == "recorded" else None,
                   "formation": team_ctx.formation.value if team_ctx and team_ctx.formation.status == "recorded" else None}
    if not required:
        missing.append("the role asked of the athlete is not recorded (coach context or roster)")
    elif not able:
        missing.append("the roles the athlete can play are not recorded in the roster")
    if phys_state == "unavailable":
        missing.append("physiological index unavailable: " + "; ".join(phys.get("reasons") or ["no reason given"]))
    if perf_state == "unavailable":
        missing.append("performance composite unavailable: " + "; ".join(perf.get("reasons") or ["no reason given"]))
    if phys_state == "elevated_unconfirmed":
        reasons.append(f"physiological index {phys['value']} reached {prof.physiology.concern_at:g} but not yet for {prof.physiology.persistence_points} consecutive points")
    if perf_state == "below_unconfirmed":
        reasons.append(f"performance composite {perf['value']} is below {prof.performance.concern_below:g} but not yet for {prof.performance.persistence_points} consecutive points")
    if perf_state == "concern":
        triggers.append("performance_decline")
        reasons.append(f"performance composite below {prof.performance.concern_below:g} for at least {prof.performance.persistence_points} consecutive review points (now {perf['value']})")
    if phys_state == "concern":
        triggers.append("physiology_elevated")
        reasons.append(f"experimental physiological index at or above {prof.physiology.concern_at:g} for at least {prof.physiology.persistence_points} consecutive review points (now {phys['value']})")
    if role_mismatch:
        triggers.append("role_mismatch")
        reasons.append(f"the role asked ({required!r}, from {role_src.replace('_', ' ')}) is not among the roles recorded for this athlete ({', '.join(me.able_roles)})")
    if perf_state == "unavailable" and perf.get("identity_uncertain_excluded") and not role_mismatch:
        reasons.append(f"{perf['identity_uncertain_excluded']} performance observations in the window could not be attributed to this player with confidence (uncertain identity)")
        return out("insufficient_evidence")
    if not triggers:
        if phys_state == "unavailable" and perf_state == "unavailable":
            reasons.append("neither the physiological index nor the performance composite is available")
            return out("insufficient_evidence")
        reasons.append("no sustained concern in the available evidence")
        return out("keep")
    if triggers == ["physiology_elevated"]:
        reasons.append("an elevated experimental index alone does not recommend a replacement; " +
                       ("performance is maintained" if perf_state in ("normal", "below_unconfirmed") else "performance evidence is unavailable"))
        return out("review_later")
    if "role_mismatch" not in triggers and phys_state == "unavailable" and prof.performance_decline_requires_physiology:
        reasons.append("the profile requires an available physiological index before a performance decline leads to considering a replacement")
        return out("review_later")
    cands = _candidates(inp, t, required, team_roster, roster)
    res["replacements"] = cands
    suitable = [c for c in cands if c["role_fit"] == "fits" and not c["blocked_reason"]]
    keep = {n.athlete_id: n.reason for n in (team_roster.keep_on_field if team_roster else [])}
    if inp.athlete_id in keep:
        constraints.append(f"coach constraint: keep on the field ({keep[inp.athlete_id]})")
        return out("keep")
    subs = team_roster.substitutions_remaining if team_roster else None
    if subs == 0:
        constraints.append("coach constraint: no substitutions remaining")
        return out("keep")
    if subs is None:
        missing.append("substitutions remaining: unknown")
    for c in cands:
        if c["blocked_reason"]:
            constraints.append(f"coach constraint: do not bring on {c['athlete_id']} ({c['blocked_reason']})")
    if not suitable:
        reasons.append("no bench athlete is recorded as able to play " + (repr(required) if required else "the required role (unknown)"))
        return out("review_later")
    before = team_roster.no_substitution_before if team_roster else None
    if before and t < before:
        constraints.append(f"coach constraint: no substitution before {iso(before)}")
        return out("review_later", before)
    holds = []
    if my_ctx and my_ctx.evidence.status == "recorded":
        for item in my_ctx.evidence.items:
            if item.observed_at is not None and item.observed_at > t:
                continue
            policy = getattr(prof.context, item.kind)
            text = f"coach-recorded {item.kind.replace('_', ' ')} evidence: {item.task} ({item.description})"
            (holds if policy == "hold_for_review" else tradeoffs).append(text)
    if my_ctx and my_ctx.partnerships.status == "recorded":
        for p in my_ctx.partnerships.items:
            partner = roster.get(p.partner_athlete_id)
            if partner and partner.status == "on_field":
                text = f"replacing this athlete separates a recorded on-field partnership with {p.partner_athlete_id}: {p.note}"
                (holds if prof.context.partnerships == "hold_for_review" else tradeoffs).append(text)
    if holds:
        tradeoffs.extend(holds)
        reasons.append("coach-recorded context favours keeping the athlete; the profile asks the coach to weigh it (no bonus or score is applied)")
        return out("review_later")
    reasons.append(f"{len(suitable)} role-compatible replacement(s) on the bench")
    return out("consider_replacement")


def substitution_review(inp: ReviewInputs) -> dict:
    head = {"recording_id": inp.recording_id, "athlete_id": inp.athlete_id, "at": iso(inp.at), "label": LABEL, "outcomes": list(OUTCOMES)}
    profiles = {"substitution": None, "physiological_index": None, "performance_rubric": None}
    if inp.substitution:
        p, sha = inp.substitution
        profiles["substitution"] = {"version": p.version, "sha256": sha, "fictional": p.fictional}
    if inp.index:
        p, sha = inp.index
        profiles["physiological_index"] = {"version": p.version, "sha256": sha, "fictional": p.fictional}
    if inp.rubric:
        p, sha = inp.rubric
        profiles["performance_rubric"] = {"version": p.version, "sha256": sha, "fictional": p.fictional}
    head["profiles"] = profiles
    head["fictional_configuration"] = any(v and v["fictional"] for v in profiles.values())
    if inp.substitution is None:
        return {**head, "outcome": "insufficient_evidence", "available": False,
                "reasons": ["no experimental substitution profile is configured (SUBSTITUTION_PROFILE_PATH); no recommendation is calculated"],
                "missing": [], "tradeoffs": [], "replacements": [], "history": []}
    if inp.athlete_id not in inp.athletes:
        return {**head, "outcome": "insufficient_evidence", "available": False, "reasons": ["the athlete is not in the recording's roster"], "missing": [], "tradeoffs": [], "replacements": [], "history": []}
    prof = inp.substitution[0]
    pts = review_points(inp, prof)
    phys = [physiology_at(inp, inp.athlete_id, t) for t in pts]
    perf = [performance_at(inp, prof, inp.athlete_id, t) for t in pts]
    ph, pr = prof.physiology, prof.performance
    phys_states = track([(t, p["value"]) for t, p in zip(pts, phys)], lambda v: v >= ph.concern_at, lambda v: v < ph.clear_below, ph.persistence_points, ph.clear_points, ("elevated_unconfirmed", ""))
    perf_states = track([(t, p["value"]) for t, p in zip(pts, perf)], lambda v: v < pr.concern_below, lambda v: v >= pr.clear_at, pr.persistence_points, pr.clear_points, ("below_unconfirmed", ""))
    decisions = [decide(inp, prof, t, s1, s2, a, b) for t, s1, s2, a, b in zip(pts, phys_states, perf_states, phys, perf)]
    emitted, last_change, held = [], None, []
    for t, d in zip(pts, decisions):
        raw = d["outcome"]
        prev = emitted[-1] if emitted else None
        if prev is not None and raw != prev and {raw, prev} == {"keep", "consider_replacement"} and last_change is not None \
                and (t - last_change).total_seconds() < prof.cooldown_seconds:
            emitted.append(prev)
            held.append(t)
            continue
        if raw != prev:
            last_change = t
        emitted.append(raw)
    now = decisions[-1]
    cooldown = None
    if emitted[-1] != now["outcome"]:
        cooldown = {"held": True, "raw_outcome": now["outcome"],
                    "note": f"the output changed to {emitted[-2] if len(emitted) > 1 else emitted[-1]} less than {prof.cooldown_seconds:g} s ago; the opposite output is held back until the cooldown ends"}
    for c in now["replacements"]:
        c["physiology"] = _brief(physiology_at(inp, c["athlete_id"], inp.at), bench=True)
        c["performance_evidence"] = len([o for o in inp.athletes[c["athlete_id"]].observations if o.period_end <= inp.at])
    history = [{"at": iso(t), "physiological_index": a["value"], "physiology_state": s1, "performance_composite": b["value"], "performance_state": s2,
                "raw_outcome": d["outcome"], "outcome": e} for t, a, b, s1, s2, d, e in zip(pts, phys, perf, phys_states, perf_states, decisions, emitted)]
    return {**head, "available": True, "outcome": emitted[-1], "raw_outcome": now["outcome"], "cooldown": cooldown,
            "triggers": now["triggers"], "reasons": now["reasons"], "missing": now["missing"], "tradeoffs": now["tradeoffs"],
            "constraints_applied": now["constraints_applied"], "review_again_at": now["review_again_at"], "role": now.get("role"),
            "replacements": now["replacements"],
            "player": {"physiology": {"state": phys_states[-1], **_brief(phys[-1])}, "performance": {"state": perf_states[-1], **perf[-1]}},
            "history": history, "points": {"step_seconds": prof.step_seconds, "count": len(pts), "first": iso(pts[0]), "last": iso(pts[-1])},
            "rules": "See app/core/substitution.py. Physiology and performance are separate inputs; coach-context evidence is shown as a tradeoff or a "
                     "reason to review, never as a bonus, and performance evidence is never counted again as context. Camera-derived movement and "
                     "distance are not inputs of this version, so uncertain camera workload cannot enter the output."}


def _brief(p: dict, bench: bool = False) -> dict:
    out = {"available": p.get("available", False), "value": p.get("value"), "timing": p.get("timing"), "input_coverage": p.get("input_coverage"),
           "reasons": p.get("reasons", []), "baseline": p.get("baseline"), "signals": p.get("signals", []), "window_sources": p.get("window_sources", {})}
    if bench and not out["available"]:
        out["note"] = "no comparison available; missing physiology is not evidence that this athlete is fresh"
    return out
