"""Blind reviewer annotation sheets for a few moments of one clip, their comparison, adjudication and conversion into
the engine's manual performance evidence (app/models/performance.py). Pure functions; scripts/annotate.py does the I/O.

A SHEET lists moments (athlete, role, frame range, what is asked, the stated criterion and any active instruction with
its stated exception) and one reviewer's labels. A blank sheet carries no labels and no model output, so each reviewer
judges without seeing anyone else's labels or the experimental result. Questions are kept apart:
  execution_pass  pass attempts and completions in the moment (execution only)
  decision        was the choice good / acceptable / poor against the stated criterion (a completed pass can be a poor
                  choice; a failed one can be a reasonable choice)
  positioning     good / acceptable / poor against the stated role-specific expectation
  tactical        followed / partially followed / not followed for the active instruction, with its stated exception
Each label is "assessed", "unable_to_assess" (with a reason, e.g. off_camera) or "no_opportunity", plus the reviewer's
confidence and rationale. Nothing here recognises passes or grades decisions automatically.

COMPARISON of two sheets of the SAME moments (refused otherwise): per question, how many moments both reviewers could
assess, how often they agreed on assessability and, where both assessed, on the judgement; every disagreement is
listed with both labels and rationales. Agreement between two reviewers is not accuracy.
ADJUDICATION is a separate record: the original sheets are never changed, and the adjudicated sheet names them by hash.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timedelta

FORMAT = "athletrix-annotation-sheet"
QUESTIONS = ("execution_pass", "decision", "positioning", "tactical")
JUDGEMENT = {"decision": ("good", "acceptable", "poor"), "positioning": ("good", "acceptable", "poor"), "tactical": ("followed", "partially_followed", "not_followed")}
UNABLE = ("not_visible", "off_camera", "occluded", "video_quality", "ambiguous", "other")
MOMENT_KEYS = ("id", "athlete_id", "role", "from_frame", "to_frame", "evidence_source", "asks", "criterion", "active_instruction", "identity", "note")


class SheetError(ValueError):
    pass


def moments_sha256(sheet: dict) -> str:
    """Hash of the moment definitions only (no labels): two reviewers' sheets must share it."""
    core = [{k: m.get(k) for k in MOMENT_KEYS} for m in sheet["moments"]]
    return hashlib.sha256(json.dumps({"clip": sheet.get("clip"), "recording_id": sheet.get("recording_id"), "moments": core}, sort_keys=True).encode()).hexdigest()


def sheet_sha256(sheet: dict) -> str:
    return hashlib.sha256(json.dumps(sheet, sort_keys=True).encode()).hexdigest()


def blank(sheet: dict, reviewer: str = "") -> dict:
    """The same moments with every label removed (what a reviewer receives)."""
    out = copy.deepcopy(sheet)
    out["reviewer"] = reviewer
    out.pop("adjudication", None)
    for m in out["moments"]:
        m["labels"] = {}
    return out


def validate(sheet: dict, *, require_labels: bool = False) -> list[str]:
    e = []
    if sheet.get("format") != FORMAT or sheet.get("format_version") != 1:
        return [f"not an {FORMAT} version 1 document"]
    clip = sheet.get("clip") or {}
    if not isinstance(clip.get("fps"), (int, float)) or clip["fps"] <= 0:
        e.append("clip.fps must be a positive number")
    ids = [m.get("id") for m in sheet.get("moments", [])]
    if not ids:
        e.append("no moments")
    if len(set(ids)) != len(ids):
        e.append("moment ids must be distinct")
    for m in sheet.get("moments", []):
        w = f"moment {m.get('id')}"
        if not m.get("athlete_id"):
            e.append(f"{w}: athlete_id is required")
        if not (isinstance(m.get("from_frame"), int) and isinstance(m.get("to_frame"), int) and 0 <= m["from_frame"] < m["to_frame"]):
            e.append(f"{w}: from_frame < to_frame (non-negative integers) is required")
        asks = m.get("asks") or []
        if not asks or any(q not in QUESTIONS for q in asks):
            e.append(f"{w}: asks must list some of {', '.join(QUESTIONS)}")
        for q in ("decision", "positioning"):
            if q in asks and not (m.get("criterion") or {}).get(q):
                e.append(f"{w}: a {q} question needs the stated criterion (criterion.{q})")
        if "tactical" in asks and not (m.get("active_instruction") or {}).get("text"):
            e.append(f"{w}: a tactical question needs the active instruction")
        labels = m.get("labels") or {}
        extra = set(labels) - set(asks)
        if extra:
            e.append(f"{w}: labels for questions not asked: {', '.join(sorted(extra))}")
        if require_labels and set(asks) - set(labels):
            e.append(f"{w}: no label for {', '.join(sorted(set(asks) - set(labels)))}")
        for q, lab in labels.items():
            a = lab.get("assessment")
            if a not in ("assessed", "unable_to_assess", "no_opportunity"):
                e.append(f"{w} {q}: assessment must be assessed, unable_to_assess or no_opportunity")
                continue
            if (a == "unable_to_assess") != (lab.get("unable_reason") is not None):
                e.append(f"{w} {q}: unable_reason is required exactly when unable_to_assess")
            elif a == "unable_to_assess" and lab["unable_reason"] not in UNABLE:
                e.append(f"{w} {q}: unable_reason must be one of {', '.join(UNABLE)}")
            if lab.get("confidence") not in (None, "high", "medium", "low"):
                e.append(f"{w} {q}: confidence must be high, medium or low")
            if a != "assessed":
                continue
            if q == "execution_pass":
                at, co = lab.get("attempts"), lab.get("completions")
                if not (isinstance(at, int) and isinstance(co, int) and at >= 1 and 0 <= co <= at):
                    e.append(f"{w} {q}: attempts >= 1 and 0 <= completions <= attempts")
            elif lab.get("judgement") not in JUDGEMENT[q]:
                e.append(f"{w} {q}: judgement must be one of {', '.join(JUDGEMENT[q])}")
    return e


def _value(q: str, lab: dict | None):
    if not lab:
        return ("missing",)
    if lab["assessment"] != "assessed":
        return (lab["assessment"],)
    return ("assessed", (lab["attempts"], lab["completions"]) if q == "execution_pass" else lab["judgement"])


def compare(a: dict, b: dict) -> dict:
    for s in (a, b):
        errs = validate(s)
        if errs:
            raise SheetError("; ".join(errs))
    if moments_sha256(a) != moments_sha256(b):
        raise SheetError("the two sheets do not describe the same moments (different clip, recording or moment definitions)")
    if (a.get("reviewer") or "") == (b.get("reviewer") or "") and a.get("reviewer"):
        raise SheetError("both sheets name the same reviewer; independent comparison needs two reviewers")
    per, disagreements = {}, []
    for q in QUESTIONS:
        asked = [m for m in a["moments"] if q in (m.get("asks") or [])]
        if not asked:
            continue
        bm = {m["id"]: m for m in b["moments"]}
        n = len(asked)
        both_assessed = same_assessability = agree = missing = 0
        for m in asked:
            la, lb = (m.get("labels") or {}).get(q), (bm[m["id"]].get("labels") or {}).get(q)
            va, vb = _value(q, la), _value(q, lb)
            if "missing" in (va[0], vb[0]):
                missing += 1
                continue
            if va[0] == vb[0]:
                same_assessability += 1
            if va[0] == vb[0] == "assessed":
                both_assessed += 1
                agree += va == vb
            if va != vb:
                disagreements.append({"moment": m["id"], "question": q, "athlete_id": m["athlete_id"],
                                      "a": {k: la.get(k) for k in ("assessment", "judgement", "attempts", "completions", "unable_reason", "confidence", "rationale") if la.get(k) is not None},
                                      "b": {k: lb.get(k) for k in ("assessment", "judgement", "attempts", "completions", "unable_reason", "confidence", "rationale") if lb.get(k) is not None}})
        per[q] = {"moments_asked": n, "unlabelled_by_either": missing, "same_assessability": same_assessability, "both_assessed": both_assessed,
                  "agreed_where_both_assessed": agree, "agreement": None if not both_assessed else round(agree / both_assessed, 3)}
    return {"reviewers": [a.get("reviewer"), b.get("reviewer")], "moments_sha256": moments_sha256(a), "sheets_sha256": [sheet_sha256(a), sheet_sha256(b)],
            "fictional": bool(a.get("fictional") or b.get("fictional")), "per_question": per, "disagreements": disagreements,
            "note": "Agreement between two reviewers on these moments; not accuracy, and moments of one clip are not independent participants."}


def adjudicate(a: dict, b: dict, decisions: list[dict], adjudicator: str) -> dict:
    """A new sheet: agreed labels kept, disagreements resolved by `decisions` [{moment, question, label, reason}], the
    rest left unresolved (no label). The originals are not modified and are named by hash."""
    cmp = compare(a, b)
    by = {(d["moment"], d["question"]): d for d in decisions}
    out = blank(a, adjudicator)
    bm = {m["id"]: m for m in b["moments"]}
    unresolved = []
    for m in out["moments"]:
        src_a = next(x for x in a["moments"] if x["id"] == m["id"])
        for q in m.get("asks") or []:
            la, lb = (src_a.get("labels") or {}).get(q), (bm[m["id"]].get("labels") or {}).get(q)
            d = by.pop((m["id"], q), None)
            if d is not None:
                m["labels"][q] = {**d["label"], "rationale": d.get("reason") or d["label"].get("rationale")}
            elif la and lb and _value(q, la) == _value(q, lb):
                m["labels"][q] = {**la, "rationale": f"agreed: {la.get('rationale') or ''} / {lb.get('rationale') or ''}".strip(" /")}
            else:
                unresolved.append(f"{m['id']}:{q}")
    if by:
        raise SheetError(f"decisions for moments/questions that are not in the sheets: {', '.join(f'{k[0]}:{k[1]}' for k in by)}")
    out["adjudication"] = {"adjudicator": adjudicator, "from_sheets_sha256": cmp["sheets_sha256"], "reviewers": cmp["reviewers"], "unresolved": unresolved,
                           "note": "The original sheets are unchanged; this sheet is a separate adjudicated record."}
    errs = validate(out)
    if errs:
        raise SheetError("; ".join(errs))
    return out


def to_observations(sheet: dict, *, reference_label: bool = True) -> list[dict]:
    """Engine performance observations (POST /recordings/{id}/performance bodies), one per labelled question."""
    errs = validate(sheet)
    if errs:
        raise SheetError("; ".join(errs))
    clip = sheet["clip"]
    if not clip.get("frame0_utc"):
        raise SheetError("clip.frame0_utc (when frame 0 happened, with its basis) is required to place moments in a recording")
    t0 = datetime.fromisoformat(clip["frame0_utc"].replace("Z", "+00:00"))
    fps = clip["fps"]
    reviewer = sheet.get("adjudication", {}).get("adjudicator") or sheet.get("reviewer") or None
    out = []
    for m in sheet["moments"]:
        start, end = t0 + timedelta(seconds=m["from_frame"] / fps), t0 + timedelta(seconds=m["to_frame"] / fps)
        frames = [{"video": clip["name"], "frame": f, "video_time_s": round(f / fps, 6), **({"video_sha256": clip["sha256"]} if clip.get("sha256") else {})}
                  for f in (m["from_frame"], m["to_frame"])]
        for q, lab in (m.get("labels") or {}).items():
            kind, area = {"execution_pass": ("pass", "execution"), "decision": ("decision", "decision_making"), "positioning": ("positioning", "movement_positioning"),
                          "tactical": ("tactical_observation", "coach_strategy")}[q]
            b = {"athlete_id": m["athlete_id"], "role": m.get("role"), "area": area, "kind": kind, "period_start": start.isoformat(), "period_end": end.isoformat(),
                 "assessment": lab["assessment"], "evidence_source": m.get("evidence_source") or "human_review_of_camera", "reference_label": reference_label,
                 "annotator": reviewer, "frames": frames, "description": f"moment {m['id']} ({q})", "confidence": lab.get("confidence"), "rationale": lab.get("rationale"),
                 "identity": m.get("identity")}
            if lab["assessment"] == "unable_to_assess":
                b["unable_reason"] = lab["unable_reason"]
            elif lab["assessment"] == "assessed":
                if q == "execution_pass":
                    b.update(attempts=lab["attempts"], completions=lab["completions"])
                elif q == "tactical":
                    ai = m["active_instruction"]
                    b.update(outcome=lab["judgement"], instruction=ai["text"], instruction_exception=ai.get("exception"))
                else:
                    j = lab["judgement"]
                    b.update(opportunities=1, rated_good=int(j == "good"), rated_acceptable=int(j == "acceptable"), rated_poor=int(j == "poor"), criterion=m["criterion"][q])
            out.append({k: v for k, v in b.items() if v is not None})
    return out
