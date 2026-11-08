"""Timing evidence for a saved prediction: was it saved before the sprint started? (pure functions)

CATEGORIES
  saved_before_recorded_start  an independent start reference (timing gate, video frame) recorded on its own clock,
                               mapped onto the engine clock with a recorded offset and uncertainty, lies after the
                               moment the prediction was saved by more than that uncertainty.
  order_unverified             the order cannot be established. Sub-cases (`basis`):
                                 confirmed_sequence   saved with "save score" and confirmed by the operator's start
                                                      marker, but no independent start reference
                                 no_mapping           a start reference exists on a device clock without a recorded mapping
                                 within_uncertainty   the gap is smaller than the mapping's uncertainty
                                 marker_only          calculated when a start marker reached the engine (no saved score;
                                                      this includes the earlier two-second grace period, never promoted)
                                 no_start             saved, but no start was recorded
  retrospective                calculated after the fact: a backdated marker, or a recorded start before the save.
POLICY (declared in the study manifest before outcomes are collected)
  independent_start_reference  only saved_before_recorded_start is eligible for the comparison
  confirmed_sequence           also order_unverified / confirmed_sequence
The engine clock is the reference; other clocks enter only through a recorded mapping.
"""

from __future__ import annotations

from datetime import datetime, timedelta

POLICIES = ("independent_start_reference", "confirmed_sequence")


def _t(v) -> datetime:
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def classify(pred: dict, start: dict | None, result: dict | None) -> dict:
    """pred / start / result: study entries as dicts (model_dump(mode='json') or StudyEntry.model_dump())."""
    p = pred.get("prediction") or {}
    trig = p.get("trigger") or "sprint_marker"
    saved_at = _t(pred["created_at"]) if trig == "score_save" else _t(p.get("cutoff") or pred["at"])
    if p.get("prospective") is False:
        return {"category": "retrospective", "basis": "backdated_marker", "detail": p.get("timing_label") or "the marker reached the engine after the stated sprint time"}
    ref = (start or {}).get("start_reference") or (result or {}).get("start_reference")
    if ref:
        if ref.get("clock_offset_s") is None:
            return {"category": "order_unverified", "basis": "no_mapping", "detail": f"start recorded by {ref['source']} on its own clock without a recorded mapping to the engine clock"}
        unc = ref.get("clock_uncertainty_s")
        if unc is None:
            return {"category": "order_unverified", "basis": "no_mapping", "detail": "the clock mapping has no stated uncertainty"}
        start_engine = _t(ref["device_time"]) + timedelta(seconds=ref["clock_offset_s"])
        gap = (start_engine - saved_at).total_seconds()
        if gap > unc:
            return {"category": "saved_before_recorded_start", "basis": ref["source"], "gap_s": round(gap, 3), "uncertainty_s": unc,
                    "detail": f"saved {gap:.2f} s before the {ref['source']} start (mapping uncertainty ±{unc:g} s)"}
        if gap < -unc:
            return {"category": "retrospective", "basis": "start_before_save", "gap_s": round(gap, 3), "detail": f"the {ref['source']} start was {-gap:.2f} s before the score was saved"}
        return {"category": "order_unverified", "basis": "within_uncertainty", "gap_s": round(gap, 3), "detail": f"the gap ({gap:.2f} s) is within the mapping uncertainty (±{unc:g} s)"}
    if trig == "score_save":
        if not start:
            return {"category": "order_unverified", "basis": "no_start", "detail": "the score was saved but no start was recorded"}
        return {"category": "order_unverified", "basis": "confirmed_sequence",
                "detail": "saved and confirmed before the operator's start marker; no independent start reference, so the physical start is not timed against it"}
    return {"category": "order_unverified", "basis": "marker_only",
            "detail": "calculated when the start marker reached the engine (no saved score; includes the earlier two-second grace period, which is never treated as verified)"}


def eligible(ev: dict, policy: str) -> bool:
    if policy == "independent_start_reference":
        return ev["category"] == "saved_before_recorded_start"
    return ev["category"] == "saved_before_recorded_start" or (ev["category"] == "order_unverified" and ev.get("basis") == "confirmed_sequence")
