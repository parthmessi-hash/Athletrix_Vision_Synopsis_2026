"""Science-fair fatigue pilot: study records, saved pre-sprint predictions, scores on demand and the pilot view.

See app/models/study.py (records) and app/core/fatigue.py (scores). The session freezes the configured fatigue profile
(FATIGUE_PROFILE_PATH) by storing its exact text; every score of that session, and every saved prediction, uses the
frozen text, never whatever is configured later. Nothing here gives substitution advice.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Response, status

from app.api.performance import parse_at
from app.api.recordings import require_member, require_recording
from app.core import clock
from app.core.clock import MAX_FUTURE_TIMESTAMP_SKEW
from app.core.config import get_settings
from app.core.fatigue import RULES_VERSION, FatigueProfileError, ScoreInputs, Wearer, iso, parse_profile, score
from app.core.knowledge import baselines_as_of
from app.db import get_database
from app.models.recording import Recording
from app.models.study import CONFIRM_MAX_AGE_S, PROSPECTIVE_TOLERANCE_S, StudyEntry, StudyEntryCreate
from app.store.baselines import baseline_store
from app.store.recordings import recording_store
from app.store.study import current, insert, new_study_id, study_store

router = APIRouter(tags=["fatigue pilot"])
FUTURE_TOLERANCE_S = 0.5
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
    rse = sorted((e for e in known if e.kind == "round_start" and e.athlete_id == athlete_id), key=lambda e: e.at)
    opened = [e.at for e in rse if e.opens_camera_reference]
    if opened:
        rs = opened[:1]
    elif any(e.opens_camera_reference is False for e in rse):
        rs = []   # rounds started without usable data never open the reference; nothing is moved to make a result available
    else:
        rs = [e.at for e in rse]   # recordings from before 2026-11-08: the first round start, as before
    cam = next((c.model_dump(mode="json") for c in (sess.clock_references if sess else []) if c.stream == "camera"), None)
    if data is None:
        lookback = max(f.get("window_s", 0) for f in prof["features"].values())
        lookback = max(lookback, prof["features"].get("recovery", {}).get("lookback_s", 0))
        t0 = min([t - timedelta(seconds=lookback + 5)] + ([rs[0] - timedelta(seconds=5)] if rs else []))
        data = (recording_store.range_with_receipts(rec.id, athlete_id, "physiological", t0, t), recording_store.range_with_receipts(rec.id, athlete_id, "tracking", t0, t))
    return ScoreInputs(athlete_id, t, knowledge, data[0], data[1], mine, excl, rs[0] if rs else None, cam,
                       reference_note=None if rs or not rse else "no round of this athlete has opened the camera reference yet (rounds started without usable data do not open it)")


READY_LOOKBACK_S = 30.0
NAMES = {"vest_only": "vest only", "camera_only": "camera only", "combined": "combined"}


def readiness(rec: Recording, entries: list[StudyEntry], athlete_id: str, t: datetime) -> dict:
    """What the wearer's data allows right now, and the operator's next action (see the module docstring of
    app/models/study.py). States, in order of checking:
      no_profile / unassigned     no study session, or the athlete is not the active wearer
      no_stream                   nothing arriving for this athlete in the last 30 s
      stale_or_unassigned         samples arrive but are stale, from another device, outside the assignment, tagged for the
                                  previous wearer, or camera samples without confirmed identity
      ready_to_start              both streams usable and no round open: start the next round
      warming_up                  a round is open and the reference or windows are still building (seconds remaining)
      ready_to_save               a round is open and the score can be saved now (with the estimates it will have)
    Stream usability ignores test-effort exclusions; the score does not."""
    fz = frozen_profile(entries)
    if fz is None:
        return {"state": "no_profile", "next": "open the study session with a configured fatigue profile (pilot_session.py session ...)"}
    p, sha, _ = fz
    cur = current(entries)
    live = [w for w in wearer_intervals(cur) if w["start"] <= t and (w["end"] is None or t < w["end"])]
    if not live or live[0]["entry"].athlete_id != athlete_id:
        return {"state": "unassigned", "next": f"assign the vest to {athlete_id} (pilot_session.py wearer ...)",
                "active_wearer": live[0]["entry"].athlete_id if live else None}
    from app.core.fatigue import freshness, select  # noqa: PLC0415
    inp = build_inputs(rec, entries, p, athlete_id, t, "as_entered")
    t0 = t - timedelta(seconds=READY_LOOKBACK_S)
    raw_p = [s for s, _ in inp.phys if t0 < s.timestamp <= t]
    raw_t = [s for s, _ in inp.track if t0 < s.timestamp <= t]
    # stream usability ignores the test-effort exclusions (readings during and just after a sprint arrive fine; the score
    # simply leaves them out): judge arrival, device, assignment and confidence only
    sel = select(dataclasses.replace(inp, exclusions=[]), p)
    fr = freshness(sel, t, p)
    w = live[0]["entry"]
    streams = {}
    for name, raw, ok in (("wearable", raw_p, fr["wearable"]["fresh"]), ("camera", raw_t, fr["camera"]["fresh"])):
        if ok:
            streams[name] = {"usable": True, "detail": f"fresh (newest {fr[name]['age_s']:.0f} s old)"}
            continue
        if not raw:
            others = []
            if name == "wearable":
                for a in rec.athlete_ids:
                    if a != athlete_id and any((s.device_id or "") == w.device_id for s, _ in recording_store.range_with_receipts(rec.id, a, "physiological", t0, t)):
                        others.append(a)
            streams[name] = {"usable": False, "detail": f"the assigned vest {w.device_id} is sending readings tagged for {', '.join(others)} (wrong wearer in the adapter)"} if others \
                else {"usable": False, "detail": f"nothing arriving in the last {READY_LOOKBACK_S:g} s"}
            continue
        why = []
        if name == "wearable":
            devs = sorted({s.device_id or "none" for s in raw if s.device_id != w.device_id})
            if devs:
                why.append(f"from another device ({', '.join(devs)}; the assigned vest is {w.device_id})")
            before = sum(1 for s in raw if s.timestamp < live[0]["start"])
            if before:
                why.append(f"{before} recent readings from before this wearer's assignment started")
        else:
            low = sum(1 for s in raw if s.tracking_confidence < p["streams"].get("min_tracking_confidence", 0.5) or (s.value_basis or "") == "predicted")
            if low:
                why.append(f"{low} low-confidence or predicted camera samples (identity not confirmed)")
        if fr[name].get("age_s") is not None and not why:
            why.append(f"stale (newest usable sample {fr[name]['age_s']:.0f} s old)")
        streams[name] = {"usable": False, "detail": "; ".join(why) or "not usable"}
    rse = [e for e in cur if e.kind == "round_start" and e.athlete_id == athlete_id]
    ended = {e.round for e in cur if e.kind == "round_end" and e.athlete_id == athlete_id}
    open_rounds = sorted(e.round for e in rse if e.round not in ended)
    during = (f" Round {open_rounds[-1]} is open: wait for the stream to recover, save anyway (the missing estimates and their "
              "reasons are recorded), or end the round as invalid with a reason.") if open_rounds else ""
    if not raw_p and not raw_t and "tagged for" not in streams["wearable"]["detail"]:
        return {"state": "no_stream", "streams": streams, "open_round": open_rounds[-1] if open_rounds else None,
                "next": ("no stream is arriving: check the vest and the camera feed" + (during if open_rounds else " before starting a round"))}
    if not all(v["usable"] for v in streams.values()):
        bad = [f"{k}: {v['detail']}" for k, v in streams.items() if not v["usable"]]
        return {"state": "stale_or_unassigned", "streams": streams, "open_round": open_rounds[-1] if open_rounds else None,
                "next": ("fix the input: " if open_rounds else "fix the input before starting: ") + "; ".join(bad) + "." + during}
    nxt_round = max([e.round for e in rse if e.round is not None] or [0]) + 1
    extra = {"open_round": open_rounds[-1] if open_rounds else None}
    if not open_rounds:
        will_open = not any(e.opens_camera_reference for e in rse) and not (rse and all(e.opens_camera_reference is None for e in rse))
        overlap = max([(b - t).total_seconds() for a, b, _ in inp.exclusions if b > t] or [0.0])
        return {"state": "ready_to_start", "streams": streams, **extra, "next_round": nxt_round, "start_opens_camera_reference": will_open,
                **({"reference_overlaps_test_exclusion_s": round(overlap, 1)} if will_open and overlap > 0 else {}),
                "next": f"both streams are usable: start round {nxt_round} now" + (
                    f" (this start opens the camera reference period of {p['reference']['camera_reference_s']:g} s" +
                    (f"; the last {overlap:.0f} s of the sprint exclusion would fall inside it, so waiting {overlap:.0f} s gives a fuller reference)"
                     if overlap > 0 else ")") if will_open else "")}
    opened = any(e.opens_camera_reference for e in rse) or (rse and all(e.opens_camera_reference is None for e in rse))
    sc = score(inp, p, sha)
    avail = {c: v["available"] for c, v in sc["candidates"].items()}
    if not opened:
        return {"state": "warming_up", "streams": streams, "estimates_available": avail, **extra,
                "next": "this athlete's rounds were all started without usable data, so the camera reference is not open: camera and combined "
                        "scores stay unavailable; end this round and start the next one with both streams usable"}
    if inp.reference_start is not None:
        remaining = (inp.reference_start + timedelta(seconds=p["reference"]["camera_reference_s"] + max(f.get("window_s", 0) for k, f in p["features"].items()
                     if k in ("output_change", "hr_per_speed"))) - t).total_seconds()
    else:
        remaining = None
    if not all(avail.values()) and remaining is not None and remaining > 0:
        return {"state": "warming_up", "streams": streams, "estimates_available": avail, "seconds_until_windows_built": round(remaining, 1), **extra,
                "next": f"windows still building (about {remaining:.0f} s): wait before saving a score for a controlled round"}
    missing = {c: (v["reasons"] or ["unavailable"])[0] for c, v in sc["candidates"].items() if not v["available"]}
    return {"state": "ready_to_save", "streams": streams, "estimates_available": avail, "unavailable_reasons": missing, **extra,
            "next": "save the score now" + ("" if not missing else " (not available: " + "; ".join(f"{NAMES.get(k, k)}: {v}" for k, v in missing.items()) + ")")}


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
    if body.at is not None and body.at > now + timedelta(seconds=FUTURE_TOLERANCE_S) and body.kind in ("round_start", "sprint_start", "sprint_end", "round_end"):
        raise HTTPException(status_code=422, detail=f"a {body.kind} cannot be marked in the future (stated {iso(body.at)}, engine time {iso(now)})")
    t = body.at or now
    check_time(rec, t)
    entries = study_store.list(rec.id)
    with get_database().read() as c:
        existing = study_store.by_token(c, rec.id, body.client_token)
    if existing is not None and body.kind == "score_save":
        if existing.kind != "prediction" or (existing.athlete_id, existing.round) != (body.athlete_id, body.round):
            raise HTTPException(status_code=409, detail=f"client_token {body.client_token!r} was already used for a different entry")
        response.status_code = status.HTTP_200_OK
        return existing
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
    if body.kind == "score_save":
        live = [w for w in wearer_intervals(current(entries)) if w["start"] <= now and (w["end"] is None or now < w["end"])]
        if not live or live[0]["entry"].athlete_id != body.athlete_id:
            raise HTTPException(status_code=409, detail=f"{body.athlete_id} is not the active wearer: assign the wearer before saving their score")
        fz = frozen_profile(entries)
        if fz is None:
            result = {"available": False, "reason": "no study session with a frozen fatigue profile: nothing can be scored"}
        else:
            p, sha, _ = fz
            with get_database().read():
                result = score(build_inputs(rec, entries, p, body.athlete_id, now, "as_entered", marks_known_by=now), p, sha)
        cands = (result or {}).get("candidates") or {}
        pred = StudyEntry(kind="prediction", client_token=body.client_token, at=now, athlete_id=body.athlete_id, round=body.round, id=new_study_id(now, "prediction"),
                          recording_id=rec.id, created_at=now, notes=body.notes,
                          prediction={"cutoff": iso(now), "trigger": "score_save", "wearer_entry_id": live[0]["entry"].id, "knowledge": "as_entered", "result": result,
                                      "rules_version": RULES_VERSION, "all_three_available": all(cands.get(k, {}).get("available") for k in ("vest_only", "camera_only", "combined")),
                                      "timing_label": "saved by the operator's 'save score' action; whether it preceded the sprint is decided by the start that confirms it",
                                      "note": "saved at the cutoff; never rewritten. Start the timed sprint only after this confirmation."})
        with get_database().transaction() as c:
            insert(c, pred)
        return pred
    if body.kind == "sprint_start" and body.confirms_prediction_id:
        saved = next((x for x in entries if x.id == body.confirms_prediction_id and x.kind == "prediction"), None)
        if saved is None or (saved.prediction or {}).get("trigger") != "score_save":
            raise HTTPException(status_code=422, detail="confirms_prediction_id must name a score saved with score_save in this recording")
        if (saved.athlete_id, saved.round) != (body.athlete_id, body.round):
            raise HTTPException(status_code=409, detail=f"that saved score belongs to {saved.athlete_id} round {saved.round}, not {body.athlete_id} round {body.round}")
        newer = [x for x in entries if x.kind == "prediction" and (x.prediction or {}).get("trigger") == "score_save" and (x.athlete_id, x.round) == (saved.athlete_id, saved.round)
                 and x.created_at > saved.created_at]
        if newer:
            raise HTTPException(status_code=409, detail=f"a newer score was saved for this round ({newer[-1].id}): confirm that one")
        live = [w for w in wearer_intervals(current(entries)) if w["start"] <= now and (w["end"] is None or now < w["end"])]
        if not live or live[0]["entry"].id != saved.prediction.get("wearer_entry_id"):
            raise HTTPException(status_code=409, detail="the wearer assignment changed since the score was saved: save the score again")
        if any(x.kind == "sprint_start" and x.confirms_prediction_id == saved.id for x in current(entries)):
            raise HTTPException(status_code=409, detail="that saved score already started a sprint")
        age = (t - saved.created_at).total_seconds()
        if age < 0:
            raise HTTPException(status_code=409, detail="the stated start is before the score was saved: that is not a pre-sprint prediction (enter the round without confirmation)")
        if age > CONFIRM_MAX_AGE_S:
            raise HTTPException(status_code=409, detail=f"the saved score is {age:.0f} s old (limit {CONFIRM_MAX_AGE_S:g} s): save the score again")
    opens, ready_doc = None, None
    if body.kind == "round_start" and frozen_profile(entries) is not None:
        with get_database().read():
            ready_doc = readiness(rec, entries, body.athlete_id, now)
        usable = ready_doc["state"] in ("warming_up", "ready_to_start", "ready_to_save")
        if not usable and not body.partial_reason:
            raise HTTPException(status_code=409, detail={"message": f"the streams are not usable ({ready_doc['state']}): {ready_doc['next']}. "
                                                                    "To start anyway, give partial_reason (it goes into the report).", "readiness": ready_doc})
        earlier_open = any(x.opens_camera_reference for x in current(entries) if x.kind == "round_start" and x.athlete_id == body.athlete_id)
        opens = usable and not earlier_open and not body.partial_reason
    e = StudyEntry(**body.model_dump(exclude={"at"}), at=t, id=new_study_id(now, body.kind), recording_id=rec.id, created_at=now, profile=profile,
                   opens_camera_reference=opens if body.kind == "round_start" and ready_doc is not None else None,
                   readiness_at_start=ready_doc if body.kind == "round_start" else None)
    pred = None
    if body.kind == "sprint_start" and not body.confirms_prediction_id:
        fz = frozen_profile(entries)
        result = None
        if fz is None:
            result = {"available": False, "reason": "no study session with a frozen fatigue profile: nothing can be scored"}
        else:
            p, sha, _ = fz
            with get_database().read():
                result = score(build_inputs(rec, entries + [e], p, body.athlete_id, t, "as_entered", marks_known_by=now), p, sha)
        lag = (now - t).total_seconds()
        prospective = lag <= PROSPECTIVE_TOLERANCE_S
        pred = StudyEntry(kind="prediction", client_token=f"pred-{e.id}", at=t, athlete_id=body.athlete_id, round=body.round, id=new_study_id(now, "prediction"),
                          recording_id=rec.id, created_at=now, prediction={"cutoff": iso(t), "sprint_start_id": e.id, "knowledge": "as_entered", "result": result,
                                                                          "trigger": "sprint_marker", "rules_version": RULES_VERSION,
                                                                          "marker_received_at": iso(now), "marker_lag_s": round(lag, 3), "prospective": prospective,
                                                                          "timing_label": ("ORDER UNVERIFIED: calculated when the sprint marker reached the engine (no score was saved and confirmed before the start)" if prospective else
                                                                                           f"RETROSPECTIVE: the marker reached the engine {lag:.1f} s after the stated sprint time; "
                                                                                           "calculated with what was entered by that time, but not captured before the sprint"),
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
    from app.core.timing_evidence import classify  # noqa: PLC0415
    by_id = {e.id: e for e in entries}
    for r in rows.values():   # the prediction of the CURRENT sprint start (a corrected mark has its own), or the confirmed saved score
        s = r.get("sprint_start")
        if s:
            st = by_id[s["id"]]
            p = by_id.get(st.confirms_prediction_id) if st.confirms_prediction_id else next((x for x in preds if x.prediction and x.prediction.get("sprint_start_id") == s["id"]), None)
            r["prediction"] = None if p is None else p.model_dump(mode="json", include={"id", "at", "created_at", "prediction"})
        if r.get("prediction"):
            res = by_id[r["sprint_result"]["id"]].model_dump(mode="json") if r.get("sprint_result") else None
            r["timing_evidence"] = classify(by_id[r["prediction"]["id"]].model_dump(mode="json"), by_id[s["id"]].model_dump(mode="json") if s else None, res)
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
        lastp = next((e for e in reversed(known) if e.kind == "prediction" and e.athlete_id == w.athlete_id), None)
        last = None if lastp is None else {"round": lastp.round, "cutoff": lastp.prediction["cutoff"], "prospective": lastp.prediction.get("prospective", True),
                                           "timing_label": lastp.prediction.get("timing_label"),
                                           "values": {c: v["value"] for c, v in ((lastp.prediction.get("result") or {}).get("candidates") or {}).items()}}
        return {**base, "wearer": {"athlete_id": w.athlete_id, "participant_code": w.participant_code, "device_id": w.device_id, "since": iso(live[0]["start"])},
                "last_saved_prediction": last, "readiness": readiness(rec, entries, w.athlete_id, t) if at is None and rec.stopped_at is None else None,
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
    changed = [k.split("_")[0] for k in ("wearable_sha256", "camera_sha256") if again["inputs"][k] != saved["inputs"][k]]
    same_inputs = not changed
    missing_baseline = any(w.baseline is None for w in inp.wearers) and (saved["features"].get("hr_strain") or {}).get("available")
    same_values = {c: again["candidates"][c]["value"] for c in again["candidates"]} == {c: saved["candidates"][c]["value"] for c in saved["candidates"]}
    st = "reproduced" if same_inputs and same_values else ("not_reproducible" if missing_baseline else ("inputs_changed" if not same_inputs else "differs"))
    return {"prediction_id": pred.id, "cutoff": pred.prediction["cutoff"], "status": st, "saved": {c: saved["candidates"][c]["value"] for c in saved["candidates"]},
            "recalculated": {c: again["candidates"][c]["value"] for c in again["candidates"]},
            "explanation": {"reproduced": "same visible inputs and the same scores",
                            "inputs_changed": f"the stored {' and '.join(changed)} samples visible at the cutoff differ from those the prediction used "
                                              f"(saved {saved['inputs']['wearable_samples']} wearable / {saved['inputs']['camera_samples']} camera samples, now "
                                              f"{again['inputs']['wearable_samples']} / {again['inputs']['camera_samples']}): the original evidence is not all here",
                            "not_reproducible": "the baseline the prediction used is not on this engine and the prediction did not record its values",
                            "differs": f"same inputs, different scores: the scoring rules changed since it was saved (rules version {pred.prediction.get('rules_version', 1)} then, "
                                       f"{RULES_VERSION} now); the saved prediction stands"}[st],
            "prospective": pred.prediction.get("prospective"), "timing_label": pred.prediction.get("timing_label"),
            "note": "The saved prediction is never rewritten."}



@router.get("/recordings/{recording_id}/study/timing-sheet")
def timing_sheet(recording_id: str) -> dict:
    """The round list for the independent timer: wearer code, round, the start and end markers and any result already
    entered (with its entry id for corrections). It contains NO scores, predictions or self-ratings (blind by design)."""
    with get_database().read():
        rec = require_recording(recording_id)
    entries = study_store.list(rec.id)
    cur = current(entries)
    code = {e.athlete_id: e.participant_code for e in entries if e.kind == "wearer" and e.participant_code}
    sess = session_of(entries)
    rows = {}
    for e in cur:
        if e.kind in ("sprint_start", "sprint_end", "sprint_result", "round_end") and e.round is not None:
            r = rows.setdefault((e.athlete_id, e.round), {"athlete_id": e.athlete_id, "participant_code": code.get(e.athlete_id), "round": e.round})
            if e.kind == "sprint_start":
                r["sprint_start_marker"] = iso(e.at)
            elif e.kind == "sprint_end":
                r["sprint_end_marker"] = iso(e.at)
            elif e.kind == "round_end" and e.invalid:
                r["round_marked_invalid"] = e.notes
            elif e.kind == "sprint_result":
                r["result"] = {"id": e.id, "time_s": e.time_s, "method": e.method, "missing_reason": e.missing_reason, "precision_s": e.precision_s,
                               "timing_uncertainty_s": e.timing_uncertainty_s, "video_sha256": e.video_sha256, "start_pts_s": e.start_pts_s, "finish_pts_s": e.finish_pts_s,
                               "notes": e.notes, "corrects": e.supersedes}
    return {"recording_id": rec.id, "session": sess.session_label if sess else None, "fictional": bool(sess and sess.fictional),
            "rounds": sorted(rows.values(), key=lambda r: (r["participant_code"] or r["athlete_id"], r["round"])),
            "note": "Blind timing sheet: no scores, predictions or self-ratings are included."}


@router.get("/recordings/{recording_id}/study/readiness")
def study_readiness(recording_id: str, athlete_id: str) -> dict:
    """Whether the athlete's streams allow a controlled round now, and the next action (no recommendation is made)."""
    with get_database().read():
        rec = require_recording(recording_id)
        require_member(rec, athlete_id)
        return {"recording_id": rec.id, "athlete_id": athlete_id, "at": iso(clock.utc_now()), **readiness(rec, study_store.list(rec.id), athlete_id, clock.utc_now())}
