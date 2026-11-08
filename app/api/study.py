"""Science-fair fatigue pilot: study records, saved pre-sprint predictions, scores on demand and the pilot view.

See app/models/study.py (records) and app/core/fatigue.py (scores). The session freezes the configured fatigue profile
(FATIGUE_PROFILE_PATH) by storing its exact text; every score of that session, and every saved prediction, uses the
frozen text, never whatever is configured later. Nothing here gives substitution advice.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Response, status

from app.api.performance import parse_at
from app.api.recordings import require_member, require_recording
from app.core import clock
from app.core.clock import MAX_FUTURE_TIMESTAMP_SKEW
from app.core.config import get_settings
from app.core.fatigue import FatigueProfileError, ScoreInputs, Wearer, iso, parse_profile, score
from app.core.knowledge import baselines_as_of
from app.db import get_database
from app.models.recording import Recording
from app.models.study import StudyEntry, StudyEntryCreate
from app.store.baselines import baseline_store
from app.store.recordings import recording_store
from app.store.study import current, insert, new_study_id, study_store

router = APIRouter(tags=["fatigue pilot"])
TREND_STEP_S = 30
TREND_SPAN_S = 600


def configured_profile_text() -> str | None:
    path = get_settings().fatigue_profile_path
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"cannot read FATIGUE_PROFILE_PATH: {exc}") from exc


def session_of(entries: list[StudyEntry]) -> StudyEntry | None:
    s = [e for e in current(entries) if e.kind == "session"]
    return s[-1] if s else None


def frozen_profile(entries: list[StudyEntry]) -> tuple[dict, str, str] | None:
    s = session_of(entries)
    if s is None or not s.profile:
        return None
    p, sha = parse_profile(s.profile["text"])
    return p, sha, s.profile["text"]


def wearer_intervals(entries: list[StudyEntry]) -> list[dict]:
    """One active wearer at a time: each wearer entry runs until the next wearer entry (of anyone) or an end entry."""
    ws = sorted((e for e in entries if e.kind == "wearer"), key=lambda e: (e.at, e.created_at, e.id))
    out = []
    for i, e in enumerate(ws):
        if e.end:
            continue
        nxt = ws[i + 1].at if i + 1 < len(ws) else None
        out.append({"entry": e, "start": e.at, "end": nxt})
    return out


def baseline_snapshot(athlete_id: str, baseline_id: str | None, t: datetime, knowledge: str) -> dict | None:
    if not baseline_id:
        return None
    bs = [b for b in baseline_store.list(athlete_id) if b.id == baseline_id]
    if knowledge == "as_entered":
        bs = baselines_as_of(bs, t)
    if not bs:
        return None
    b = bs[0]
    hr = b.stats.get("heart_rate_bpm")
    if hr is None or not hr.sufficient:
        return {"id": b.id, "rest_hr_bpm": None, "n": 0, "status": b.status}
    return {"id": b.id, "rest_hr_bpm": round(hr.mean, 3), "n": hr.included, "status": b.status}


def build_inputs(rec: Recording, entries: list[StudyEntry], prof: dict, athlete_id: str, t: datetime, knowledge: str,
                 data: tuple[list, list] | None = None, marks_known_by: datetime | None = None) -> ScoreInputs:
    """Test-effort exclusions use every sprint mark known when the score is calculated (marks_known_by; None = all):
    excluding more data can never leak an outcome. Everything else follows the knowledge mode at t."""
    known = current(entries, t if knowledge == "as_entered" else None)
    marks = current(entries, marks_known_by)
    sess = session_of(known)
    mine = []
    for w in wearer_intervals(known):
        e = w["entry"]
        if e.athlete_id != athlete_id:
            continue
        mine.append(Wearer(w["start"], w["end"], e.device_id, e.participant_code, baseline_snapshot(athlete_id, e.baseline_id, t, knowledge), e.hr_max_bpm, e.hr_max_origin))
    ex = prof["test_effort_exclusion"]
    starts = {e.round: e for e in marks if e.kind == "sprint_start" and e.athlete_id == athlete_id}
    ends = {e.round: e for e in marks if e.kind == "sprint_end" and e.athlete_id == athlete_id}
    excl = []
    for r, s in sorted(starts.items()):
        end = ends[r].at if r in ends else s.at + timedelta(seconds=ex["assumed_duration_if_no_end_s"])
        excl.append((s.at - timedelta(seconds=ex["pre_s"]), end + timedelta(seconds=ex["post_s"]), f"timed test sprint, round {r}" + ("" if r in ends else " (end not marked: assumed duration)")))
    rs = sorted(e.at for e in known if e.kind == "round_start" and e.athlete_id == athlete_id)
    cam = next((c.model_dump(mode="json") for c in (sess.clock_references if sess else []) if c.stream == "camera"), None)
    if data is None:
        lookback = max(f.get("window_s", 0) for f in prof["features"].values())
        lookback = max(lookback, prof["features"].get("recovery", {}).get("lookback_s", 0))
        t0 = min([t - timedelta(seconds=lookback + 5)] + ([rs[0] - timedelta(seconds=5)] if rs else []))
        data = (recording_store.range_with_receipts(rec.id, athlete_id, "physiological", t0, t), recording_store.range_with_receipts(rec.id, athlete_id, "tracking", t0, t))
    return ScoreInputs(athlete_id, t, knowledge, data[0], data[1], mine, excl, rs[0] if rs else None, cam)


def check_time(rec: Recording, t: datetime) -> None:
    latest = rec.stopped_at if rec.stopped_at is not None else clock.utc_now() + MAX_FUTURE_TIMESTAMP_SKEW
    if not (rec.started_at <= t <= latest):
        raise HTTPException(status_code=422, detail=f"the time must lie within recording '{rec.id}'")


@router.post("/recordings/{recording_id}/study", status_code=status.HTTP_201_CREATED, response_model=StudyEntry,
             responses={200: {"description": "This client_token was already stored: the stored entry, unchanged"}})
def add_study_entry(recording_id: str, body: StudyEntryCreate, response: Response) -> StudyEntry:
    now = clock.utc_now()
    with get_database().read():
        rec = require_recording(recording_id)
        if body.athlete_id:
            require_member(rec, body.athlete_id)
    t = body.at or now
    check_time(rec, t)
    entries = study_store.list(rec.id)
    with get_database().read() as c:
        existing = study_store.by_token(c, rec.id, body.client_token)
    if existing is not None:
        if existing.model_dump(include=set(StudyEntryCreate.model_fields) - {"at"}) != body.model_dump(exclude={"at"}) or (body.at and body.at != existing.at):
            raise HTTPException(status_code=409, detail=f"client_token {body.client_token!r} was already used for a different entry")
        response.status_code = status.HTTP_200_OK
        return existing
    if body.supersedes:
        old = next((e for e in entries if e.id == body.supersedes), None)
        if old is None or old.kind != body.kind or old.athlete_id != body.athlete_id:
            raise HTTPException(status_code=422, detail="supersedes must name an entry of the same kind and participant in this recording")
        if any(e.supersedes == old.id for e in entries):
            raise HTTPException(status_code=409, detail="that entry was already corrected; correct the latest version")
    profile = None
    if body.kind == "session":
        if session_of(entries) is not None and not body.supersedes:
            raise HTTPException(status_code=409, detail="this recording already has a study session; correct it with supersedes")
        text = configured_profile_text()
        if text is not None:
            try:
                p, sha = parse_profile(text)
            except FatigueProfileError as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from exc
            profile = {"text": text, "sha256": sha, "version": p["version"], "fictional": p["fictional"]}
    if body.kind == "wearer" and not body.end:
        b = [x for x in baseline_store.list(body.athlete_id) if x.id == body.baseline_id]
        if not b:
            raise HTTPException(status_code=422, detail=f"baseline {body.baseline_id!r} is not a baseline of {body.athlete_id}: a wearer never uses another participant's baseline")
    e = StudyEntry(**body.model_dump(exclude={"at"}), at=t, id=new_study_id(now, body.kind), recording_id=rec.id, created_at=now, profile=profile)
    pred = None
    if body.kind == "sprint_start":
        fz = frozen_profile(entries)
        result = None
        if fz is None:
            result = {"available": False, "reason": "no study session with a frozen fatigue profile: nothing can be scored"}
        else:
            p, sha, _ = fz
            with get_database().read():
                result = score(build_inputs(rec, entries + [e], p, body.athlete_id, t, "as_entered", marks_known_by=now), p, sha)
        pred = StudyEntry(kind="prediction", client_token=f"pred-{e.id}", at=t, athlete_id=body.athlete_id, round=body.round, id=new_study_id(now, "prediction"),
                          recording_id=rec.id, created_at=now, prediction={"cutoff": iso(t), "sprint_start_id": e.id, "knowledge": "as_entered", "result": result,
                                                                          "note": "saved at the cutoff; never rewritten. A retrospective view is a separate calculation."})
    with get_database().transaction() as c:
        insert(c, e)
        if pred is not None:
            insert(c, pred)
    return e


@router.get("/recordings/{recording_id}/study")
def list_study(recording_id: str) -> dict:
    with get_database().read():
        rec = require_recording(recording_id)
    entries = study_store.list(rec.id)
    cur = current(entries)
    rows = {}
    for e in cur:
        if e.round is None or e.kind not in ("round_start", "round_end", "sprint_start", "sprint_end", "sprint_result", "self_rating", "prediction"):
            continue
        r = rows.setdefault((e.athlete_id, e.round), {"athlete_id": e.athlete_id, "round": e.round})
        r[e.kind] = e.model_dump(mode="json", include={"id", "at", "time_s", "method", "value", "scale", "missing_reason", "notes", "prediction", "supersedes"})
    preds = [e for e in entries if e.kind == "prediction"]
    for r in rows.values():   # the prediction of the CURRENT sprint start (a corrected mark has its own)
        s = r.get("sprint_start")
        if s:
            p = next((x for x in preds if x.prediction and x.prediction.get("sprint_start_id") == s["id"]), None)
            r["prediction"] = None if p is None else p.model_dump(mode="json", include={"id", "at", "prediction"})
    sess = session_of(entries)
    return {"recording_id": rec.id, "session": None if sess is None else sess.model_dump(mode="json", exclude={"profile"}) | {"profile": None if not sess.profile else {k: v for k, v in sess.profile.items() if k != "text"}},
            "wearers": [{"athlete_id": w["entry"].athlete_id, "participant_code": w["entry"].participant_code, "device_id": w["entry"].device_id, "start": iso(w["start"]),
                         "end": iso(w["end"]), "baseline_id": w["entry"].baseline_id} for w in wearer_intervals(cur)],
            "rounds": sorted(rows.values(), key=lambda r: (r["athlete_id"] or "", r["round"])), "entries": [e.model_dump(mode="json", exclude={"profile"}) for e in entries],
            "note": "entries are never edited; corrections supersede and the history stays. Predictions are engine-written at each sprint cutoff."}


def _score_at(rec, entries, athlete_id, t, knowledge, data=None) -> dict:
    fz = frozen_profile(entries)
    if fz is None:
        return {"available": False, "reason": "no study session with a frozen fatigue profile (FATIGUE_PROFILE_PATH was unset when the session was created)"}
    p, sha, _ = fz
    return score(build_inputs(rec, entries, p, athlete_id, t, knowledge, data), p, sha)


@router.get("/recordings/{recording_id}/study/score")
def study_score(recording_id: str, athlete_id: str, at: str | None = None, knowledge: Literal["as_entered", "retrospective"] = "as_entered") -> dict:
    with get_database().read():
        rec = require_recording(recording_id)
        require_member(rec, athlete_id)
        t = parse_at(at) or (rec.stopped_at or clock.utc_now())
        check_time(rec, t)
        return _score_at(rec, study_store.list(rec.id), athlete_id, t, knowledge)


@router.get("/recordings/{recording_id}/study/pilot")
def pilot_view(recording_id: str, at: str | None = None, trend_step_s: int = Query(TREND_STEP_S, ge=5, le=300), trend_span_s: int = Query(TREND_SPAN_S, ge=0, le=3600)) -> dict:
    """The current wearer's latest experimental estimate, recent trend, main factors and missing-data reasons."""
    captured = clock.utc_now()
    with get_database().read():
        rec = require_recording(recording_id)
        t = parse_at(at) or (min(captured, rec.stopped_at) if rec.stopped_at else captured)
        check_time(rec, t)
        entries = study_store.list(rec.id)
        known = current(entries, t)
        live = [w for w in wearer_intervals(known) if w["start"] <= t and (w["end"] is None or t < w["end"])]
        sess = session_of(known)
        configured = configured_profile_text()
        base = {"recording_id": rec.id, "recording_status": rec.status, "mode": "live" if rec.stopped_at is None and at is None else "recorded",
                "captured_at": iso(captured), "at": iso(t), "fictional": bool(sess and sess.fictional) or bool(sess and sess.profile and sess.profile.get("fictional")),
                "session": None if sess is None else {"label": sess.session_label, "protocol": sess.protocol, "fictional": sess.fictional,
                                                     "profile": None if not sess.profile else {"version": sess.profile["version"], "sha256": sess.profile["sha256"], "fictional": sess.profile["fictional"]}},
                "profile_differs_from_configured": bool(sess and sess.profile and configured is not None and configured != sess.profile["text"]),
                "note": "Experimental estimate relative to the wearer's own baseline. No substitution advice; the coach decides."}
        if not live:
            return {**base, "wearer": None, "estimate": None, "trend": [], "reason": "no active wearer at this time"}
        w = live[0]["entry"]
        now = _score_at(rec, entries, w.athlete_id, t, "as_entered")
        trend = []
        if trend_span_s and frozen_profile(entries):
            p = frozen_profile(entries)[0]
            first = max(live[0]["start"], t - timedelta(seconds=trend_span_s))
            lookback = max(max(f.get("window_s", 0) for f in p["features"].values()), p["features"].get("recovery", {}).get("lookback_s", 0))
            rs = [e.at for e in known if e.kind == "round_start" and e.athlete_id == w.athlete_id]
            t0 = min([first - timedelta(seconds=lookback + 5)] + ([min(rs) - timedelta(seconds=5)] if rs else []))
            data = (recording_store.range_with_receipts(rec.id, w.athlete_id, "physiological", t0, t), recording_store.range_with_receipts(rec.id, w.athlete_id, "tracking", t0, t))
            k = 0
            while True:
                tp = t - timedelta(seconds=k * trend_step_s)
                if tp < first:
                    break
                r = score(build_inputs(rec, entries, p, w.athlete_id, tp, "as_entered", data), p, frozen_profile(entries)[1])
                trend.append({"at": iso(tp), **{c: r["candidates"][c]["value"] for c in r["candidates"]}})
                k += 1
            trend.reverse()
        newest = None
        if now.get("streams"):
            newest = max((x.get("newest") for x in now["streams"].values() if x.get("newest")), default=None)
        recs = [r for s, r in recording_store.range_with_receipts(rec.id, w.athlete_id, "physiological", t - timedelta(seconds=60), t) if r and r.get("received_at")]
        return {**base, "wearer": {"athlete_id": w.athlete_id, "participant_code": w.participant_code, "device_id": w.device_id, "since": iso(live[0]["start"])},
                "estimate": now, "trend": trend, "newest_sample": newest,
                "newest_receipt": max((r["received_at"] for r in recs), default=None)}


@router.get("/recordings/{recording_id}/study/predictions/{prediction_id}/reproduce")
def reproduce_prediction(recording_id: str, prediction_id: str) -> dict:
    """Recalculate a saved prediction from the stored data as it was entered by its cutoff, with the session's frozen
    profile, and compare. A difference means stored inputs visible at the cutoff changed; the saved prediction stays."""
    with get_database().read():
        rec = require_recording(recording_id)
        entries = study_store.list(rec.id)
        pred = next((e for e in entries if e.id == prediction_id and e.kind == "prediction"), None)
        if pred is None:
            raise HTTPException(status_code=404, detail=f"prediction {prediction_id!r} does not exist in {rec.id!r}")
        saved = pred.prediction["result"]
        fz = frozen_profile(entries)
        if fz is None or saved.get("candidates") is None:
            return {"prediction_id": pred.id, "status": "not_reproducible", "explanation": "no frozen profile or nothing was scored"}
        p, sha, _ = fz
        t = datetime.fromisoformat(pred.prediction["cutoff"].replace("Z", "+00:00"))
        inp = build_inputs(rec, entries, p, pred.athlete_id, t, "as_entered", marks_known_by=pred.created_at)
        # baselines may not travel with an imported recording: use the values the prediction recorded
        sb = (saved["features"].get("hr_strain") or {}).get("reference") or {}
        for w in inp.wearers:
            if w.baseline is None and sb.get("baseline_id"):
                ok = (saved["features"].get("hr_strain") or {}).get("reason", "").find("baseline") < 0
                w.baseline = {"id": sb["baseline_id"], "rest_hr_bpm": sb["rest_hr_bpm"], "n": None, "status": p["baseline"]["require_status"] if ok else "unknown",
                              "source": "the values recorded in the prediction (the baseline's own recording is not on this engine)"}
        again = score(inp, p, sha)
    same_inputs = (again["inputs"]["wearable_sha256"], again["inputs"]["camera_sha256"]) == (saved["inputs"]["wearable_sha256"], saved["inputs"]["camera_sha256"])
    same_values = {c: again["candidates"][c]["value"] for c in again["candidates"]} == {c: saved["candidates"][c]["value"] for c in saved["candidates"]}
    st = "reproduced" if same_inputs and same_values else ("inputs_changed" if not same_inputs else "differs")
    return {"prediction_id": pred.id, "cutoff": pred.prediction["cutoff"], "status": st, "saved": {c: saved["candidates"][c]["value"] for c in saved["candidates"]},
            "recalculated": {c: again["candidates"][c]["value"] for c in again["candidates"]},
            "explanation": {"reproduced": "same visible inputs and the same scores", "inputs_changed": "the stored samples visible at the cutoff changed since the prediction was saved",
                            "differs": "same inputs, different scores: the scoring code changed"}[st],
            "note": "The saved prediction is never rewritten."}
