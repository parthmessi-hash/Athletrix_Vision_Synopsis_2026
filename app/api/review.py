"""Experimental review layer: athlete baselines, the physiological index, roster state, the substitution review and saved
(reproducible) reviews. See app/core/physiology_index.py, app/core/substitution.py and app/models/review.py.

Every calculation here needs an explicitly configured profile (PHYSIOLOGICAL_INDEX_PROFILE_PATH,
SUBSTITUTION_PROFILE_PATH, PERFORMANCE_RUBRIC_PATH); without one the result says it is unavailable. The engine ships no
profile; FICTIONAL examples live in scripts/examples/review/ and scripts/examples/performance/.
"""

from __future__ import annotations

import json
from datetime import datetime

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Response, status

from app.api.performance import parse_at
from app.api.recordings import require_member, require_recording
from app.core import clock
from app.core.clock import MAX_FUTURE_TIMESTAMP_SKEW
from app.core.config import get_settings
from app.core.knowledge import baselines_as_of
from app.core.performance import RubricError, parse_rubric
from app.core.performance import review as performance_review
from app.core.physiology_index import ProfileError, baseline_stats, compute_index, history, iso, parse_profile, samples_digest, select_baseline
from app.core.substitution import AthleteData, ReviewInputs, substitution_review
from app.db import get_database
from app.models.baseline import Baseline, BaselineCreate, BaselineDecision, RecordedDecision
from app.models.context import ContextEntry
from app.models.performance import PerformanceObservation
from app.models.recording import Recording
from app.models.review import ReviewRequest, SavedReview
from app.models.response import CoachResponse, RecommendationRef, ResponseCreate
from app.models.roster import RosterCreate, RosterEntry
from app.models.substitution import SubstitutionProfile
from app.store.baselines import baseline_store, new_baseline_id
from app.store.context import context_store
from app.store.performance import performance_store
from app.store.recordings import recording_store
from app.store.reviews import new_review_id, review_store
from app.store.responses import insert as response_insert
from app.store.responses import new_response_id, response_store
from app.store.roster import effective_roster, new_roster_id, roster_store
from app.store.roster import insert as roster_insert

router = APIRouter(tags=["experimental review"])


# ---- configuration -----------------------------------------------------------------------------------------------------

def _read(path: str | None) -> str | None:
    if not path:
        return None
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"cannot read the configured file {path}: {exc}") from exc


def configured_docs() -> dict[str, str | None]:
    """The exact text of the configured profiles (stored with saved reviews so they can be reproduced)."""
    s = get_settings()
    return {"substitution": _read(s.substitution_profile_path), "physiological_index": _read(s.physiological_index_profile_path),
            "performance_rubric": _read(s.performance_rubric_path)}


def parse_docs(docs: dict[str, str | None]) -> dict:
    out = {}
    try:
        out["physiological_index"] = parse_profile(docs["physiological_index"].encode()) if docs.get("physiological_index") else None
        out["performance_rubric"] = parse_rubric(docs["performance_rubric"].encode()) if docs.get("performance_rubric") else None
    except (ProfileError, RubricError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if docs.get("substitution"):
        import hashlib  # noqa: PLC0415
        try:
            out["substitution"] = (SubstitutionProfile.model_validate_json(docs["substitution"]), hashlib.sha256(docs["substitution"].encode()).hexdigest())
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"the substitution profile is invalid: {exc}") from exc
    else:
        out["substitution"] = None
    return out


def review_time(rec: Recording, at: datetime | None) -> datetime:
    now = clock.utc_now()
    t = at or (rec.stopped_at or now)
    latest = rec.stopped_at if rec.stopped_at is not None else now + MAX_FUTURE_TIMESTAMP_SKEW
    if not (rec.started_at <= t <= latest):
        raise HTTPException(status_code=422, detail=f"the review time must lie within recording '{rec.id}' ({iso(rec.started_at)} to {iso(latest)})")
    return t


# ---- baselines ------------------------------------------------------------------------------------------------------------

@router.post("/athletes/{athlete_id}/baselines", response_model=Baseline, status_code=status.HTTP_201_CREATED,
             responses={404: {"description": "Unknown recording or athlete not in its roster"}, 422: {"description": "Invalid definition, active recording or ranges outside it"}})
def create_baseline(athlete_id: str, body: BaselineCreate) -> Baseline:
    """Calculate and store a baseline from a stopped recording. Statistics are written once; the review status starts
    as "pending_review"."""
    now = clock.utc_now()
    with get_database().read():
        rec = require_recording(body.source_recording_id)
        require_member(rec, athlete_id)
        if rec.stopped_at is None:
            raise HTTPException(status_code=422, detail="a baseline is taken from a stopped recording, so its data can no longer change")
        if any(r.start < rec.started_at or r.end > rec.stopped_at for r in body.ranges):
            raise HTTPException(status_code=422, detail=f"every range must lie within recording '{rec.id}'")
        samples = recording_store.all(rec.id, athlete_id, "physiological")
    stats = baseline_stats(body, samples)
    b = Baseline(**body.model_dump(), id=new_baseline_id(now), athlete_id=athlete_id, version=1, created_at=now,
                 data_start=min(r.start for r in body.ranges), data_end=max(r.end for r in body.ranges), stats=stats)
    return baseline_store.add(b)


@router.get("/athletes/{athlete_id}/baselines", response_model=list[Baseline])
def list_baselines(athlete_id: str) -> list[Baseline]:
    return baseline_store.list(athlete_id)


@router.post("/athletes/{athlete_id}/baselines/{baseline_id}/decision", response_model=Baseline)
def decide_baseline(athlete_id: str, baseline_id: str, body: BaselineDecision) -> Baseline:
    """Record a person's review decision (accepted or rejected, with a note). Kept with every earlier decision. A
    decision is not a scientific validation."""
    b = baseline_store.decide(athlete_id, baseline_id, RecordedDecision(**body.model_dump(), decided_at=clock.utc_now()))
    if b is None:
        raise HTTPException(status_code=404, detail=f"baseline {baseline_id!r} does not exist for athlete {athlete_id!r}")
    return b


# ---- physiological index ----------------------------------------------------------------------------------------------------

def _index_profile():
    return parse_docs({"physiological_index": _read(get_settings().physiological_index_profile_path)})["physiological_index"]


UNCONFIGURED = "no physiological index profile is configured (PHYSIOLOGICAL_INDEX_PROFILE_PATH); the engine ships none and no real-athlete profile exists"


@router.get("/recordings/{recording_id}/athletes/{athlete_id}/physiological-index")
def physiological_index(recording_id: str, athlete_id: str, at: str | None = None, baseline_id: str | None = None,
                        knowledge: Literal["retrospective", "as_entered"] = "retrospective") -> dict:
    """The experimental index at `at` (default: the recording's stop, or now). Uses only samples available at `at`."""
    prof = _index_profile()
    with get_database().read():
        rec = require_recording(recording_id)
        require_member(rec, athlete_id)
        t = review_time(rec, parse_at(at))
        samples = recording_store.all_with_receipts(rec.id, athlete_id, "physiological")
        baselines = baseline_store.list(athlete_id)
    if prof is None:
        return {"recording_id": rec.id, "athlete_id": athlete_id, "at": iso(t), "available": False, "value": None, "reasons": [UNCONFIGURED]}
    profile, sha = prof
    if baseline_id is not None:
        chosen = [b for b in baselines if b.id == baseline_id]
        if not chosen:
            raise HTTPException(status_code=404, detail=f"baseline {baseline_id!r} does not exist for athlete {athlete_id!r}")
        baselines = chosen
    if knowledge == "as_entered":
        baselines = baselines_as_of(baselines, t)
    b, timing, why = select_baseline(baselines, profile, t)
    return {"recording_id": rec.id, "athlete_id": athlete_id, "knowledge": knowledge, **compute_index(profile, sha, b, timing, samples, t, why)}


@router.get("/recordings/{recording_id}/athletes/{athlete_id}/physiological-index/history")
def physiological_index_history(recording_id: str, athlete_id: str, step_s: float = Query(60, ge=5, le=1800)) -> dict:
    """The index at every step from the recording's start + window to its stop (or now); each point uses only data available then."""
    prof = _index_profile()
    with get_database().read():
        rec = require_recording(recording_id)
        require_member(rec, athlete_id)
        end = review_time(rec, None)
        samples = recording_store.all_with_receipts(rec.id, athlete_id, "physiological")
        baselines = baseline_store.list(athlete_id)
    if prof is None:
        return {"recording_id": rec.id, "athlete_id": athlete_id, "available": False, "points": [], "reasons": [UNCONFIGURED]}
    pts = history(prof[0], prof[1], baselines, samples, rec.started_at, end, step_s)
    if len(pts) > 2000:
        raise HTTPException(status_code=422, detail="too many points; use a larger step_s")
    return {"recording_id": rec.id, "athlete_id": athlete_id, "available": True, "step_s": step_s,
            "points": [{k: p[k] for k in ("at", "available", "value", "timing", "input_coverage", "reasons")} for p in pts]}


# ---- roster state -------------------------------------------------------------------------------------------------------------

@router.post("/recordings/{recording_id}/roster", response_model=RosterEntry, status_code=status.HTTP_201_CREATED)
def add_roster_entry(recording_id: str, body: RosterCreate) -> RosterEntry:
    """Store a roster-state (athlete) or match-constraint (team) entry. Never edited; a correction is a newer entry."""
    now = clock.utc_now()
    with get_database().read():
        rec = require_recording(recording_id)
        for a in [body.athlete_id] * (body.athlete_id is not None) + [n.athlete_id for n in body.keep_on_field + body.do_not_bring_on]:
            require_member(rec, a)
    latest = rec.stopped_at if rec.stopped_at is not None else now + MAX_FUTURE_TIMESTAMP_SKEW
    if not (rec.started_at <= body.effective_from <= latest):
        raise HTTPException(status_code=422, detail=f"effective_from must lie within recording '{rec.id}'")
    return roster_store.add(rec.id, body, now)


@router.get("/recordings/{recording_id}/roster", response_model=list[RosterEntry])
def list_roster(recording_id: str) -> list[RosterEntry]:
    with get_database().read():
        rec = require_recording(recording_id)
        return roster_store.list(rec.id)


@router.get("/recordings/{recording_id}/roster/effective")
def effective_roster_at(recording_id: str, at: str) -> dict:
    t = parse_at(at)
    with get_database().read():
        rec = require_recording(recording_id)
        team, athletes = effective_roster(roster_store.list(rec.id), t)
    return {"recording_id": rec.id, "at": iso(t), "team": team.model_dump(mode="json") if team else None,
            "athletes": {a: e.model_dump(mode="json") for a, e in sorted(athletes.items())},
            "unknown": [a.id for a in rec.athletes if a.id not in athletes]}


# ---- substitution review ----------------------------------------------------------------------------------------------------

def gather(rec: Recording, athlete_id: str, at: datetime, parsed: dict, knowledge: str = "retrospective",
           unreadable: dict[str, str] | None = None) -> tuple[ReviewInputs, dict]:
    """The review inputs from this engine's stored data, and the evidence snapshot to store with a saved review. With
    `unreadable`, an athlete whose stored records cannot be read is left out and recorded there instead of failing all."""
    athletes, snap = {}, {}
    for a in rec.athletes:
        try:
            samples = [(s, r) for s, r in recording_store.all_with_receipts(rec.id, a.id, "physiological") if s.timestamp <= at]
            d = AthleteData(a, samples, baseline_store.list(a.id), performance_store.list(rec.id, a.id))
        except Exception as exc:  # noqa: BLE001  a damaged stored record: report it, never guess
            if unreadable is None:
                raise
            unreadable[a.id] = f"stored records could not be read ({type(exc).__name__}: {str(exc)[:160]})"
            continue
        athletes[a.id] = d
        snap[a.id] = {"baselines": [b.model_dump(mode="json") for b in d.baselines], "observations": [o.model_dump(mode="json") for o in d.observations],
                      "samples_up_to_at": len(samples), "samples_sha256": samples_digest(samples)}
    context, roster = context_store.list(rec.id), roster_store.list(rec.id)
    inp = ReviewInputs(rec.id, rec.started_at, rec.stopped_at, athlete_id, at, athletes, context, roster,
                       parsed["substitution"], parsed["physiological_index"], parsed["performance_rubric"], knowledge)
    return inp, {"athletes": snap, "context": [e.model_dump(mode="json") for e in context], "roster": [e.model_dump(mode="json") for e in roster]}


@router.get("/recordings/{recording_id}/athletes/{athlete_id}/substitution-review")
def get_substitution_review(recording_id: str, athlete_id: str, at: str | None = None,
                            knowledge: Literal["retrospective", "as_entered"] = "retrospective") -> dict:
    """The experimental keep / consider replacement / review later / insufficient evidence output at `at`. Retrospective
    (default) uses everything stored now and says what was entered after `at`; as_entered uses only what had been entered."""
    parsed = parse_docs(configured_docs())
    with get_database().read():
        rec = require_recording(recording_id)
        require_member(rec, athlete_id)
        t = review_time(rec, parse_at(at))
        inp, _ = gather(rec, athlete_id, t, parsed, knowledge)
    return substitution_review(inp)


# ---- saved reviews ----------------------------------------------------------------------------------------------------------

def compute(kind: str, rec: Recording, athlete_id: str, at: datetime, window_s: float | None, docs: dict, evidence: dict | None,
            knowledge: str = "retrospective") -> tuple[dict, dict]:
    """(result, inputs). With `evidence` (a saved snapshot), its copies are used instead of the current stored evidence."""
    parsed = parse_docs(docs)
    if kind == "performance":
        obs = ([PerformanceObservation.model_validate(o) for o in evidence["observations"]] if evidence else performance_store.list(rec.id, athlete_id))
        rub = parsed["performance_rubric"] or (None, None)
        return performance_review(obs, rub[0], rub[1], at, window_s, knowledge), {"observations": [o.model_dump(mode="json") for o in obs]}
    samples = [(s, r) for s, r in recording_store.all_with_receipts(rec.id, athlete_id, "physiological") if s.timestamp <= at]
    if kind == "physiological_index":
        baselines = [Baseline.model_validate(b) for b in evidence["baselines"]] if evidence else baseline_store.list(athlete_id)
        snap = {"baselines": [b.model_dump(mode="json") for b in baselines], "samples_up_to_at": len(samples), "samples_sha256": samples_digest(samples)}
        if parsed["physiological_index"] is None:
            return {"available": False, "value": None, "reasons": [UNCONFIGURED]}, snap
        p, sha = parsed["physiological_index"]
        b, timing, why = select_baseline(baselines_as_of(baselines, at) if knowledge == "as_entered" else baselines, p, at)
        return {"knowledge": knowledge, **compute_index(p, sha, b, timing, samples, at, why)}, snap
    inp, snap = gather(rec, athlete_id, at, parsed, knowledge)
    if evidence:
        for aid, d in inp.athletes.items():
            e = evidence["athletes"].get(aid, {"baselines": [], "observations": []})
            d.baselines = [Baseline.model_validate(b) for b in e["baselines"]]
            d.observations = [PerformanceObservation.model_validate(o) for o in e["observations"]]
        inp.context = [ContextEntry.model_validate(e) for e in evidence["context"]]
        inp.roster = [RosterEntry.model_validate(e) for e in evidence["roster"]]
        snap = {**evidence, "athletes": {aid: {**evidence["athletes"].get(aid, {}), "samples_up_to_at": s["samples_up_to_at"], "samples_sha256": s["samples_sha256"]}
                                         for aid, s in snap["athletes"].items()}}
    return substitution_review(inp), snap


@router.post("/recordings/{recording_id}/athletes/{athlete_id}/reviews", response_model=SavedReview, status_code=status.HTTP_201_CREATED)
def save_review(recording_id: str, athlete_id: str, body: ReviewRequest) -> SavedReview:
    """Calculate a review with the configured profiles and store it with their exact content and the evidence used."""
    now = clock.utc_now()
    docs = configured_docs()
    with get_database().read():
        rec = require_recording(recording_id)
        require_member(rec, athlete_id)
        t = review_time(rec, body.at)
        result, evidence = compute(body.kind, rec, athlete_id, t, body.window_s, docs, None, body.knowledge)
    inputs = {"configuration": docs, "window_s": body.window_s, "knowledge": body.knowledge, "evidence": evidence,
              "note": "Exact configuration text and evidence copies; samples stay in the recording and are checked against samples_sha256 when reproducing."}
    return review_store.add(SavedReview(id=new_review_id(now), recording_id=rec.id, athlete_id=athlete_id, kind=body.kind, at=t, created_at=now,
                                        label=body.label, inputs=inputs, result=result))


@router.get("/recordings/{recording_id}/reviews", response_model=list[SavedReview])
def list_reviews(recording_id: str, athlete_id: str | None = None) -> list[SavedReview]:
    with get_database().read():
        rec = require_recording(recording_id)
        return review_store.list(rec.id, athlete_id)


@router.get("/recordings/{recording_id}/reviews/{review_id}/reproduce")
def reproduce_review(recording_id: str, review_id: str) -> dict:
    """Re-run a saved review from its stored configuration and evidence (never the current settings) and compare."""
    with get_database().read():
        rec = require_recording(recording_id)
        saved = review_store.get(rec.id, review_id)
        if saved is None:
            raise HTTPException(status_code=404, detail=f"review {review_id!r} does not exist for recording {rec.id!r}")
        result, evidence = compute(saved.kind, rec, saved.athlete_id, saved.at, saved.inputs.get("window_s"), saved.inputs["configuration"], saved.inputs["evidence"],
                                   saved.inputs.get("knowledge", "retrospective"))
        current_docs = configured_docs()
    stored_ev = saved.inputs["evidence"]
    digests = {}
    if saved.kind == "physiological_index":
        digests = {saved.athlete_id: (stored_ev["samples_sha256"], evidence["samples_sha256"])}
    elif saved.kind == "substitution":
        digests = {a: (e.get("samples_sha256"), evidence["athletes"].get(a, {}).get("samples_sha256")) for a, e in stored_ev["athletes"].items()}
    missing = sorted(a for a, (x, y) in digests.items() if y is None)
    changed = sorted(a for a, (x, y) in digests.items() if y is not None and x != y)

    def comparable(r: dict) -> str:   # an imported copy has a new recording id; everything else must match
        return json.dumps({k: v for k, v in r.items() if k != "recording_id"}, sort_keys=True, default=str)
    same = comparable(result) == comparable(saved.result)
    if missing or changed:
        status_, why = "evidence_changed", ("recorded samples are missing for " + ", ".join(missing) + "; " if missing else "") + \
            ("recorded samples differ from the saved digest for " + ", ".join(changed) if changed else "")
    elif not same:
        status_, why = "differs", "same configuration and evidence, different result: the calculation code has changed since the review was saved"
    else:
        status_, why = "reproduced", "same configuration, evidence copies and recorded samples give the same result"
    return {"review_id": saved.id, "kind": saved.kind, "at": iso(saved.at), "status": status_, "explanation": why.rstrip("; "),
            "matches": same and not (missing or changed), "result": result, "saved_result": saved.result,
            "recorded_samples_changed_for": changed, "recorded_samples_missing_for": missing, "knowledge": saved.inputs.get("knowledge", "retrospective"),
            "configuration_differs_from_current": {k: v != current_docs.get(k) for k, v in saved.inputs["configuration"].items()},
            "note": "Re-calculated from the saved configuration and evidence copies with this engine's code; samples re-read from the recording. "
                    "The engine's current configuration is not used."}


# ---- team review ------------------------------------------------------------------------------------------------------------

TEAM_REVIEW_MAX_ATHLETES = 40
RAW_SIGNALS = ("heart_rate_bpm", "breathing_rate_bpm", "hrv_rmssd_ms", "hrv_sdnn_ms", "skin_temperature_c", "spo2_percent", "signal_quality")


def latest_reading(d: AthleteData, t: datetime, knowledge: str) -> dict | None:
    """The newest recorded physiological sample usable at t, with its three times kept apart: when it was measured
    (timestamp), when this engine received it (receipt metadata, if recorded) and how old it is at t. In as_entered mode a
    sample received after t is not usable; a sample without receipt metadata is shown with its availability unknown."""
    for s, receipt in reversed(d.samples):
        received = datetime.fromisoformat(receipt["received_at"].replace("Z", "+00:00")) if receipt else None
        if knowledge == "as_entered" and received is not None and received > t:
            continue
        values = {k: getattr(s, k) for k in RAW_SIGNALS if getattr(s, k) is not None}
        return {"observed_at": iso(s.timestamp), "received_at": iso(received) if received else None,
                "availability": "received_by_review_time" if received else "unknown: no receipt metadata (assumed available at its timestamp)",
                "age_s": round((t - s.timestamp).total_seconds(), 3), "device_id": s.device_id, "source": str(s.source), "values": values,
                "label": "recorded reading (not an interpretation)"}
    return None


@router.get("/recordings/{recording_id}/team-review")
def team_review(recording_id: str, knowledge: Literal["retrospective", "as_entered"] = Query(..., description="Required: as_entered for live use, retrospective for later review (labelled)."),
                at: str | None = None, budget_s: float = Query(10.0, gt=0, le=60)) -> dict:
    """Every roster athlete of the recording at ONE captured review time, with one set of profiles: roster state and role,
    the newest recorded reading (observation, receipt and age kept apart), the experimental physiological index and the
    performance composite with their coverage, the experimental substitution output and its reasons, and the missing
    inputs. One request for the whole team. An athlete whose records cannot be read, or who is not reached within the
    time budget, is reported as not reviewed, never with another athlete's or an earlier result."""
    import time as _time  # noqa: PLC0415
    started = _time.perf_counter()
    captured_at = clock.utc_now()
    parsed = parse_docs(configured_docs())
    unreadable: dict[str, str] = {}
    with get_database().read():
        rec = require_recording(recording_id)
        if len(rec.athletes) > TEAM_REVIEW_MAX_ATHLETES:
            raise HTTPException(status_code=422, detail=f"the team review covers at most {TEAM_REVIEW_MAX_ATHLETES} athletes; this recording has {len(rec.athletes)}")
        t = review_time(rec, parse_at(at)) if at else min(captured_at, rec.stopped_at) if rec.stopped_at else captured_at
        inp, _ = gather(rec, rec.athletes[0].id, t, parsed, knowledge, unreadable)
    rows = []
    for a in rec.athletes:
        row = {"athlete": a.model_dump(mode="json")}
        if a.id in unreadable:
            rows.append({**row, "reviewed": False, "error": unreadable[a.id]})
            continue
        if _time.perf_counter() - started > budget_s:
            rows.append({**row, "reviewed": False, "error": f"not reviewed: the {budget_s:g} s time budget of this request ran out"})
            continue
        try:
            from dataclasses import replace  # noqa: PLC0415
            r = substitution_review(replace(inp, athlete_id=a.id))
        except Exception as exc:  # noqa: BLE001  one athlete's failure never becomes a result, and never stops the others
            rows.append({**row, "reviewed": False, "error": f"review failed ({type(exc).__name__}: {str(exc)[:160]})"})
            continue
        from app.core.substitution import known  # noqa: PLC0415
        team_ros, ros = effective_roster(known(inp, inp.roster, t), t)
        entry = ros.get(a.id)
        d = inp.athletes[a.id]
        obs = known(inp, d.observations, t)
        latest_obs = max((o for o in obs if o.period_end <= t), key=lambda o: o.period_end, default=None)
        player = r.get("player") or {}
        rows.append({**row, "reviewed": True,
                     "roster": {"status": entry.status if entry else "unknown", "current_role": entry.current_role if entry else None,
                                "able_roles": entry.able_roles if entry else [], "entered_at": iso(entry.created_at) if entry else None},
                     "latest_reading": latest_reading(d, t, knowledge),
                     "latest_performance_observation": None if latest_obs is None else {
                         "period_end": iso(latest_obs.period_end), "entered_at": iso(latest_obs.created_at), "age_s": round((t - latest_obs.period_end).total_seconds(), 3)},
                     "physiological_index": player.get("physiology"), "performance": player.get("performance"),
                     "outcome": r["outcome"], "reason": (r.get("reasons") or ["no reason given"])[0], "review": r})
    reviewed = [x for x in rows if x["reviewed"]]
    return {"recording_id": rec.id, "recording_status": rec.status, "captured_at": iso(captured_at), "at": iso(t), "knowledge": knowledge,
            "knowledge_label": ("as entered: only evidence entered and samples received by the review time; samples without receipt metadata are assumed available at their timestamps"
                                if knowledge == "as_entered" else "retrospective: may use evidence entered after the review time (each athlete's review says what)"),
            "profiles": reviewed[0]["review"].get("profiles") if reviewed else None,
            "fictional_configuration": any(x["review"].get("fictional_configuration") for x in reviewed),
            "counts": {"athletes": len(rows), "reviewed": len(reviewed), "not_reviewed": len(rows) - len(reviewed),
                       "outcomes": {o: sum(1 for x in reviewed if x["outcome"] == o) for o in ("keep", "consider_replacement", "review_later", "insufficient_evidence")}},
            "compute_ms": round(1000 * (_time.perf_counter() - started), 1), "athletes": rows,
            "note": "Athletes are listed in roster order, never ranked; an unavailable value is not a zero. Experimental decision support: the coach decides."}


# ---- coach responses and lineup changes -----------------------------------------------------------------------------------

@router.post("/recordings/{recording_id}/responses", status_code=status.HTTP_201_CREATED,
             responses={200: {"description": "The same client_token was already saved: the stored response, unchanged"}, 409: {"description": "Roster state or a stated constraint does not allow the lineup change"}})
def add_response(recording_id: str, body: ResponseCreate, response: Response) -> CoachResponse:
    """Record a coach / operator response, or an actual lineup change (app/models/response.py). Idempotent per
    client_token. A lineup change is checked against the roster state and constraints recorded for its effective time;
    nothing is assumed where they are unknown."""
    now = clock.utc_now()
    with get_database().read():
        rec = require_recording(recording_id)
        require_member(rec, body.athlete_id)
    recommendation = None
    if body.saved_review_id is not None:
        saved = review_store.get(rec.id, body.saved_review_id)
        if saved is None:
            raise HTTPException(status_code=404, detail=f"saved review {body.saved_review_id!r} does not exist for recording {rec.id!r}")
        if saved.athlete_id != body.athlete_id or saved.kind != "substitution":
            raise HTTPException(status_code=422, detail="the saved review must be a substitution review of the same athlete")
        recommendation = RecommendationRef(saved_review_id=saved.id, outcome=saved.result.get("outcome"), review_at=saved.at,
                                           knowledge=saved.inputs.get("knowledge", "retrospective"), profiles=saved.result.get("profiles"))
    entries, warnings = [], []
    if body.lineup is not None:
        lu = body.lineup
        require_member(rec, lu.incoming_athlete_id)
        latest = rec.stopped_at if rec.stopped_at is not None else now + MAX_FUTURE_TIMESTAMP_SKEW
        if not (rec.started_at <= lu.effective_from <= latest):
            raise HTTPException(status_code=422, detail=f"effective_from must lie within recording '{rec.id}'")
        team, ros = effective_roster(roster_store.list(rec.id), lu.effective_from)
        out_e, in_e = ros.get(lu.outgoing_athlete_id), ros.get(lu.incoming_athlete_id)
        if out_e is None or out_e.status != "on_field":
            raise HTTPException(status_code=409, detail=f"{lu.outgoing_athlete_id} is not recorded on the field at {iso(lu.effective_from)} (status: {out_e.status if out_e else 'unknown'})")
        if in_e is None or in_e.status != "bench":
            raise HTTPException(status_code=409, detail=f"{lu.incoming_athlete_id} is not recorded on the bench at {iso(lu.effective_from)} (status: {in_e.status if in_e else 'unknown'})")
        if team is not None:
            blocked = {n.athlete_id: n.reason for n in team.do_not_bring_on}
            keep = {n.athlete_id: n.reason for n in team.keep_on_field}
            if lu.incoming_athlete_id in blocked:
                raise HTTPException(status_code=409, detail=f"coach constraint: do not bring on {lu.incoming_athlete_id} ({blocked[lu.incoming_athlete_id]}); record a new match-constraint entry first if that changed")
            if lu.outgoing_athlete_id in keep:
                raise HTTPException(status_code=409, detail=f"coach constraint: keep {lu.outgoing_athlete_id} on the field ({keep[lu.outgoing_athlete_id]}); record a new match-constraint entry first if that changed")
            if team.substitutions_remaining == 0:
                raise HTTPException(status_code=409, detail="the recorded match constraints say no substitutions remain")
            if team.no_substitution_before is not None and lu.effective_from < team.no_substitution_before:
                raise HTTPException(status_code=409, detail=f"coach constraint: no substitution before {iso(team.no_substitution_before)}")
        role = lu.incoming_role or out_e.current_role
        if role is None:
            warnings.append("no role recorded for the incoming athlete")
        elif in_e.able_roles and role.strip().casefold() not in [r.strip().casefold() for r in in_e.able_roles]:
            if not lu.accept_role_mismatch:
                raise HTTPException(status_code=409, detail=f"{lu.incoming_athlete_id}'s recorded roles ({', '.join(in_e.able_roles)}) do not include {role!r}; set accept_role_mismatch to record it anyway")
            warnings.append(f"role {role!r} is not among {lu.incoming_athlete_id}'s recorded roles; recorded at the coach's request")
        elif not in_e.able_roles:
            warnings.append(f"{lu.incoming_athlete_id}'s roles are not recorded; role fit unknown")
        if team is None or team.substitutions_remaining is None:
            warnings.append("substitutions remaining are not recorded; nothing was decremented")
        note = f"lineup change by response token {body.client_token}"
        entries.append(RosterCreate(athlete_id=lu.outgoing_athlete_id, effective_from=lu.effective_from, status="unavailable", able_roles=out_e.able_roles,
                                    source="head_coach", recorded_by=body.recorded_by, note=f"substituted off ({note})"))
        entries.append(RosterCreate(athlete_id=lu.incoming_athlete_id, effective_from=lu.effective_from, status="on_field", current_role=role, able_roles=in_e.able_roles,
                                    source="head_coach", recorded_by=body.recorded_by, note=f"substituted on ({note})"))
        if team is not None and team.substitutions_remaining is not None:
            entries.append(RosterCreate(athlete_id=None, effective_from=lu.effective_from, substitutions_remaining=team.substitutions_remaining - 1,
                                        no_substitution_before=team.no_substitution_before, keep_on_field=team.keep_on_field, do_not_bring_on=team.do_not_bring_on,
                                        source="head_coach", recorded_by=body.recorded_by, note=f"one substitution used ({note})"))
    with get_database().transaction() as c:
        existing = response_store.by_token(c, rec.id, body.client_token)
        if existing is not None:
            if existing.model_dump(include=set(ResponseCreate.model_fields)) != body.model_dump():
                raise HTTPException(status_code=409, detail=f"client_token {body.client_token!r} was already used for a different response")
            response.status_code = status.HTTP_200_OK
            return existing
        ids = []
        for e in entries:
            entry = RosterEntry(**e.model_dump(), id=new_roster_id(now), recording_id=rec.id, created_at=now)
            while entry.id in ids or c.execute("SELECT 1 FROM recording_roster WHERE id = ?", (entry.id,)).fetchone():
                entry = entry.model_copy(update={"id": new_roster_id(now)})
            roster_insert(c, entry)
            ids.append(entry.id)
        r = CoachResponse(**body.model_dump(), id=new_response_id(now), recording_id=rec.id, created_at=now, recommendation=recommendation, roster_entry_ids=ids, warnings=warnings)
        while c.execute("SELECT 1 FROM recording_responses WHERE id = ?", (r.id,)).fetchone():
            r = r.model_copy(update={"id": new_response_id(now)})
        response_insert(c, r)
    return r


@router.get("/recordings/{recording_id}/responses", response_model=list[CoachResponse])
def list_responses(recording_id: str, athlete_id: str | None = None) -> list[CoachResponse]:
    with get_database().read():
        rec = require_recording(recording_id)
        return response_store.list(rec.id, athlete_id)


# ---- profile and baseline inspection ----------------------------------------------------------------------------------------

@router.get("/profiles")
def configured_profiles() -> dict:
    """The experimental profiles this engine is configured with (none by default): kind, version, SHA-256, fictional flag,
    what they set, errors, warnings and compatibility problems between them. Read-only."""
    from app.core.profiles import compatibility, inspect  # noqa: PLC0415
    s = get_settings()
    paths = {"physiological_index": s.physiological_index_profile_path, "substitution": s.substitution_profile_path, "performance_rubric": s.performance_rubric_path}
    found = {}
    for kind, path in paths.items():
        if not path:
            found[kind] = None
            continue
        try:
            with open(path, "rb") as f:
                r = inspect(f.read(), path.rsplit("/", 1)[-1])
        except OSError as exc:
            r = {"name": path.rsplit("/", 1)[-1], "kind": kind, "valid": False, "errors": [f"cannot read: {exc}"], "warnings": [], "summary": {}}
        if r.get("kind") not in (None, kind):
            r["errors"].append(f"configured as {kind} but the file is a {r['kind']}")
            r["valid"] = False
        found[kind] = r
    return {"profiles": found, "compatibility": compatibility(found),
            "note": "No profile ships with the engine; an unset profile means its calculation is unavailable. Saved reviews keep the exact text they were calculated with."}


@router.post("/athletes/{athlete_id}/baselines/preview")
def preview_baseline(athlete_id: str, body: BaselineCreate) -> dict:
    """The statistics a baseline with this definition would have, without storing anything."""
    with get_database().read():
        rec = require_recording(body.source_recording_id)
        require_member(rec, athlete_id)
        if rec.stopped_at is None:
            raise HTTPException(status_code=422, detail="a baseline is taken from a stopped recording, so its data can no longer change")
        if any(r.start < rec.started_at or r.end > rec.stopped_at for r in body.ranges):
            raise HTTPException(status_code=422, detail=f"every range must lie within recording '{rec.id}'")
        samples = recording_store.all(rec.id, athlete_id, "physiological")
    stats = baseline_stats(body, samples)
    return {"athlete_id": athlete_id, "stored": False, "stats": {k: v.model_dump(mode="json") for k, v in stats.items()},
            "data_start": iso(min(r.start for r in body.ranges)), "data_end": iso(max(r.end for r in body.ranges)),
            "note": "Preview only: nothing was stored. Creating it stores these statistics with review status pending_review."}


@router.get("/athletes/{athlete_id}/baselines/compatibility")
def baseline_compatibility(athlete_id: str, at: str) -> dict:
    """For each of the athlete's baselines: could the configured index profile use it at `at`, and if not, why."""
    t = parse_at(at)
    prof = _index_profile()
    baselines = baseline_store.list(athlete_id)
    if prof is None:
        return {"athlete_id": athlete_id, "at": iso(t), "profile": None, "baselines": [{"id": b.id, "version": b.version, "usable": False, "reasons": [UNCONFIGURED]} for b in baselines]}
    profile, sha = prof
    rows = []
    for b in baselines:
        chosen, timing, why = select_baseline([b], profile, t)
        reasons = list(why)
        for spec in profile.signals:
            st = b.stats.get(spec.signal)
            if st is None:
                reasons.append(f"has no {spec.signal}")
            elif not st.sufficient:
                reasons.append(f"{spec.signal}: {st.insufficient_reason}")
            elif st.included < profile.baseline.min_samples:
                reasons.append(f"{spec.signal}: {st.included} values, the profile needs {profile.baseline.min_samples}")
        rows.append({"id": b.id, "version": b.version, "status": b.status, "condition": b.condition, "usable": chosen is not None, "timing": timing, "reasons": reasons})
    return {"athlete_id": athlete_id, "at": iso(t), "profile": {"version": profile.version, "sha256": sha, "fictional": profile.fictional}, "baselines": rows,
            "note": "A baseline can be usable overall while some of its signals are not; each signal's problems are listed."}
