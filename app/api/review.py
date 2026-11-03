"""Experimental review layer: athlete baselines, the physiological index, roster state, the substitution review and saved
(reproducible) reviews. See app/core/physiology_index.py, app/core/substitution.py and app/models/review.py.

Every calculation here needs an explicitly configured profile (PHYSIOLOGICAL_INDEX_PROFILE_PATH,
SUBSTITUTION_PROFILE_PATH, PERFORMANCE_RUBRIC_PATH); without one the result says it is unavailable. The engine ships no
profile; FICTIONAL examples live in scripts/examples/review/ and scripts/examples/performance/.
"""

from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, status

from app.api.performance import parse_at
from app.api.recordings import require_member, require_recording
from app.core import clock
from app.core.clock import MAX_FUTURE_TIMESTAMP_SKEW
from app.core.config import get_settings
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
from app.models.roster import RosterCreate, RosterEntry
from app.models.substitution import SubstitutionProfile
from app.store.baselines import baseline_store, new_baseline_id
from app.store.context import context_store
from app.store.performance import performance_store
from app.store.recordings import recording_store
from app.store.reviews import new_review_id, review_store
from app.store.roster import effective_roster, roster_store

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
def physiological_index(recording_id: str, athlete_id: str, at: str | None = None, baseline_id: str | None = None) -> dict:
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
    b, timing, why = select_baseline(baselines, profile, t)
    return {"recording_id": rec.id, "athlete_id": athlete_id, **compute_index(profile, sha, b, timing, samples, t, why)}


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

def gather(rec: Recording, athlete_id: str, at: datetime, parsed: dict) -> tuple[ReviewInputs, dict]:
    """The review inputs from this engine's stored data, and the evidence snapshot to store with a saved review."""
    athletes, snap = {}, {}
    for a in rec.athletes:
        samples = [(s, r) for s, r in recording_store.all_with_receipts(rec.id, a.id, "physiological") if s.timestamp <= at]
        d = AthleteData(a, samples, baseline_store.list(a.id), performance_store.list(rec.id, a.id))
        athletes[a.id] = d
        snap[a.id] = {"baselines": [b.model_dump(mode="json") for b in d.baselines], "observations": [o.model_dump(mode="json") for o in d.observations],
                      "samples_up_to_at": len(samples), "samples_sha256": samples_digest(samples)}
    context, roster = context_store.list(rec.id), roster_store.list(rec.id)
    inp = ReviewInputs(rec.id, rec.started_at, rec.stopped_at, athlete_id, at, athletes, context, roster,
                       parsed["substitution"], parsed["physiological_index"], parsed["performance_rubric"])
    return inp, {"athletes": snap, "context": [e.model_dump(mode="json") for e in context], "roster": [e.model_dump(mode="json") for e in roster]}


@router.get("/recordings/{recording_id}/athletes/{athlete_id}/substitution-review")
def get_substitution_review(recording_id: str, athlete_id: str, at: str | None = None) -> dict:
    """The experimental keep / consider replacement / review later / insufficient evidence output at `at`."""
    parsed = parse_docs(configured_docs())
    with get_database().read():
        rec = require_recording(recording_id)
        require_member(rec, athlete_id)
        t = review_time(rec, parse_at(at))
        inp, _ = gather(rec, athlete_id, t, parsed)
    return substitution_review(inp)


# ---- saved reviews ----------------------------------------------------------------------------------------------------------

def compute(kind: str, rec: Recording, athlete_id: str, at: datetime, window_s: float | None, docs: dict, evidence: dict | None) -> tuple[dict, dict]:
    """(result, inputs). With `evidence` (a saved snapshot), its copies are used instead of the current stored evidence."""
    parsed = parse_docs(docs)
    if kind == "performance":
        obs = ([PerformanceObservation.model_validate(o) for o in evidence["observations"]] if evidence else performance_store.list(rec.id, athlete_id))
        rub = parsed["performance_rubric"] or (None, None)
        return performance_review(obs, rub[0], rub[1], at, window_s), {"observations": [o.model_dump(mode="json") for o in obs]}
    samples = [(s, r) for s, r in recording_store.all_with_receipts(rec.id, athlete_id, "physiological") if s.timestamp <= at]
    if kind == "physiological_index":
        baselines = [Baseline.model_validate(b) for b in evidence["baselines"]] if evidence else baseline_store.list(athlete_id)
        snap = {"baselines": [b.model_dump(mode="json") for b in baselines], "samples_up_to_at": len(samples), "samples_sha256": samples_digest(samples)}
        if parsed["physiological_index"] is None:
            return {"available": False, "value": None, "reasons": [UNCONFIGURED]}, snap
        p, sha = parsed["physiological_index"]
        b, timing, why = select_baseline(baselines, p, at)
        return compute_index(p, sha, b, timing, samples, at, why), snap
    inp, snap = gather(rec, athlete_id, at, parsed)
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
        result, evidence = compute(body.kind, rec, athlete_id, t, body.window_s, docs, None)
    inputs = {"configuration": docs, "window_s": body.window_s, "evidence": evidence,
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
        result, evidence = compute(saved.kind, rec, saved.athlete_id, saved.at, saved.inputs.get("window_s"), saved.inputs["configuration"], saved.inputs["evidence"])
        current_docs = configured_docs()
    stored_ev = saved.inputs["evidence"]
    digests = {}
    if saved.kind == "physiological_index":
        digests = {saved.athlete_id: (stored_ev["samples_sha256"], evidence["samples_sha256"])}
    elif saved.kind == "substitution":
        digests = {a: (e.get("samples_sha256"), evidence["athletes"].get(a, {}).get("samples_sha256")) for a, e in stored_ev["athletes"].items()}
    changed = sorted(a for a, (x, y) in digests.items() if x != y)
    def comparable(r: dict) -> str:   # an imported copy has a new recording id; everything else must match
        return json.dumps({k: v for k, v in r.items() if k != "recording_id"}, sort_keys=True, default=str)
    same = comparable(result) == comparable(saved.result)
    return {"review_id": saved.id, "kind": saved.kind, "at": iso(saved.at), "matches": same, "result": result, "saved_result": saved.result,
            "recorded_samples_changed_for": changed,
            "configuration_differs_from_current": {k: v != current_docs.get(k) for k, v in saved.inputs["configuration"].items()},
            "note": "Re-calculated from the saved configuration and evidence copies with this engine's code; samples re-read from the recording. "
                    "The engine's current configuration is not used."}
