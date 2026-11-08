"""Evidence quality per player and per source at one review time (pure functions; the API layer reads the data).

For each source the result keeps three times apart: when the newest value was OBSERVED (its own timestamp), when this
engine RECEIVED it (receipt metadata; unknown for rows without it, e.g. imports of older packages, which are then
assumed available at their own timestamp and labelled so) and its AGE at the review time. Status, in this order:
  not_used      the session plan (roster entry inputs) says this source is not part of the session for the athlete
  unknown       nothing received and no plan stated
  unavailable   nothing received although the plan expects it
  disconnected  the device's latest status report says connected = false (reported at or after the newest value), or
                nothing new for longer than disconnected_after
  stale         newest value older than stale_after
  delayed       fresh by age, but the newest value arrived more than delayed_after after it was observed, or (as entered)
                values in the window arrived only after the review time
  present       otherwise
Thresholds are adjustable engineering defaults (app/core/config.py), not the team's freshness requirements.

Counts in the evidence window: observed values; an EXPECTED count only when the plan states a sampling interval (it is
null otherwise, never invented); values excluded by the index rules, by reason; values that arrived after the review
time; retries (identical re-sends, kept once with their first receipt), replacements (a different payload for the same
instant) and rejected writes (the engine refused the request: nothing was stored). A missing observation is simply
absent: the engine cannot tell an unsent value from one lost on the way, and says so.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta

LABELS = {"wearable": "Wearable", "tracking": "Tracking (camera)", "performance": "Performance entries"}


def _t(v: str) -> datetime:
    return datetime.fromisoformat(v.replace("Z", "+00:00"))


def iso(t: datetime | None) -> str | None:
    return None if t is None else t.isoformat().replace("+00:00", "Z")


def stream_status(use: str | None, newest: tuple | None, t: datetime, knowledge: str, *, stale_after: float, disconnected_after: float,
                  delayed_after: float, device_status=None, late_in_window: int = 0) -> dict:
    """Status of one sample stream (see the module docstring). `newest` is (sample, receipt-or-None) usable at t."""
    if use == "not_used":
        return {"status": "not_used", "basis": "the session plan says this source is not used for this athlete"}
    if newest is None:
        return {"status": "unavailable" if use == "expected" else "unknown",
                "basis": "nothing received for this athlete in this recording" + ("" if use == "expected" else " and no session plan states whether it is expected")}
    s, receipt = newest
    received = _t(receipt["received_at"]) if receipt and receipt.get("received_at") else None
    age = (t - s.timestamp).total_seconds()
    out = {"last_observed_at": iso(s.timestamp), "last_received_at": iso(received), "age_s": round(age, 3),
           "receipt_delay_s": None if received is None else round((received - s.timestamp).total_seconds(), 3),
           "availability": "received_by_review_time" if received is not None else "assumed: no receipt metadata (available at its own timestamp, not verified)",
           "thresholds_s": {"stale_after": stale_after, "disconnected_after": disconnected_after, "delayed_after": delayed_after}}
    if device_status is not None and device_status.connected is False and device_status.timestamp >= s.timestamp and device_status.timestamp <= t:
        return {**out, "status": "disconnected", "basis": f"device {device_status.device_id} reported connected = false at {iso(device_status.timestamp)}"}
    if age > disconnected_after:
        return {**out, "status": "disconnected", "basis": f"nothing new for {age:.0f} s (labelled disconnected after {disconnected_after:g} s)"}
    if age > stale_after:
        return {**out, "status": "stale", "basis": f"newest value {age:.0f} s old (stale after {stale_after:g} s)"}
    if (out["receipt_delay_s"] or 0) > delayed_after or (knowledge == "as_entered" and late_in_window):
        why = (f"newest value arrived {out['receipt_delay_s']:.1f} s after it was observed" if (out["receipt_delay_s"] or 0) > delayed_after
               else f"{late_in_window} values in the window arrived after the review time")
        return {**out, "status": "delayed", "basis": why}
    return {**out, "status": "present", "basis": f"newest value {age:.0f} s old"}


def window_counts(samples: list[tuple], t: datetime, window_s: float, interval_s: float | None, knowledge: str) -> dict:
    """Counts over (t - window_s, t] of (sample, receipt) pairs whose timestamp is at most t."""
    start = t - timedelta(seconds=window_s)
    inside = [(s, r) for s, r in samples if start < s.timestamp <= t]
    late = sum(1 for s, r in inside if r and r.get("received_at") and _t(r["received_at"]) > t)
    usable = len(inside) - (late if knowledge == "as_entered" else 0)
    expected = None if not interval_s else window_s / interval_s
    return {"window_seconds": window_s, "observed": usable, "received_after_review_time": late,
            "expected": None if expected is None else round(expected, 1),
            "expected_basis": f"stated sampling interval {interval_s:g} s" if interval_s else "unknown: no sampling interval is stated in the session plan",
            "coverage": None if expected is None else round(min(1.0, usable / expected), 3),
            "retries": sum(int((r or {}).get("duplicate_arrivals", 0)) for _, r in inside),
            "replacements": sum(int((r or {}).get("replaced_earlier", 0)) for _, r in inside),
            "no_receipt_metadata": sum(1 for _, r in inside if not r)}


def tracking_continuity(samples: list[tuple], t: datetime, window_s: float, max_gap_s: float, min_conf: float) -> dict:
    """Segments, gaps longer than max_gap_s and low-confidence or non-observed values in the window (camera evidence)."""
    start = t - timedelta(seconds=window_s)
    inside = sorted((s for s, _ in samples if start < s.timestamp <= t), key=lambda s: s.timestamp)
    gaps, prev = [], start
    for s in inside:
        g = (s.timestamp - prev).total_seconds()
        if g > max_gap_s:
            gaps.append({"from": iso(prev), "to": iso(s.timestamp), "seconds": round(g, 2)})
        prev = s.timestamp
    tail = (t - prev).total_seconds()
    if tail > max_gap_s:
        gaps.append({"from": iso(prev), "to": iso(t), "seconds": round(tail, 2), "open": True})
    segs = [s.segment_id for s in inside]
    changes = sum(1 for a, b in zip(segs, segs[1:]) if a != b)
    return {"segments": sorted({x for x in segs if x}), "segment_changes": changes, "segment_not_reported": sum(1 for x in segs if x is None),
            "gaps": gaps[-5:], "gap_count": len(gaps), "uncovered_seconds": round(sum(g["seconds"] for g in gaps), 2),
            "value_basis": dict(Counter(s.value_basis or "not_reported" for s in inside)),
            "low_confidence": sum(1 for s in inside if s.tracking_confidence < min_conf),
            "note": "Camera samples exist only for confirmed track assignments; gaps include unassigned or unconfirmed intervals, lost tracks and "
                    "pauses in sending, which the engine cannot tell apart. The identity-review export says which were unconfirmed."}


def player_evidence(*, plan, phys_samples: list[tuple], phys_index: dict | None, tracking_window: list[tuple], tracking_newest: tuple | None,
                    performance: dict | None, latest_perf, review: dict | None, t: datetime, knowledge: str, index_profile, settings,
                    device_statuses: dict, rejections: dict, recording_stopped: bool) -> dict:
    stale_w = index_profile.staleness_seconds if index_profile is not None else settings.evidence_stale_after_seconds
    window_w = index_profile.window_seconds if index_profile is not None else 60.0
    usable = [(s, r) for s, r in phys_samples if s.timestamp <= t and not (knowledge == "as_entered" and r and r.get("received_at") and _t(r["received_at"]) > t)]
    newest = usable[-1] if usable else None
    dev = device_statuses.get(newest[0].device_id) if newest and newest[0].device_id else None
    late = sum(1 for s, r in phys_samples if t - timedelta(seconds=window_w) < s.timestamp <= t and r and r.get("received_at") and _t(r["received_at"]) > t)
    wear = {"source": "wearable", "plan": getattr(plan, "physiology", None) if plan else None,
            **stream_status(getattr(plan, "physiology", None) if plan else None, newest, t, knowledge, stale_after=stale_w,
                            disconnected_after=settings.evidence_disconnected_after_seconds, delayed_after=settings.evidence_delayed_after_seconds,
                            device_status=dev, late_in_window=late),
            "window": window_counts(phys_samples, t, window_w, getattr(plan, "physiology_interval_s", None) if plan else None, knowledge),
            "devices": sorted({s.device_id for s, _ in usable[-50:] if s.device_id}),
            "device_status": None if dev is None else {"device_id": dev.device_id, "connected": dev.connected, "reported_at": iso(dev.timestamp), "battery_percent": dev.battery_percent},
            "rejected_writes": rejections.get("physiological", {"count": 0})}
    if phys_index is not None:
        excl = {}
        for sg in phys_index.get("signals") or []:
            for why, n in (sg.get("window_excluded") or {}).items():
                excl.setdefault(why, 0)
                excl[why] += n
        wear["window"]["excluded_by_index_rules"] = excl
    tw = settings.evidence_stale_after_seconds
    track = {"source": "tracking", "plan": getattr(plan, "camera", None) if plan else None,
             **stream_status(getattr(plan, "camera", None) if plan else None, tracking_newest, t, knowledge, stale_after=tw,
                             disconnected_after=settings.evidence_disconnected_after_seconds, delayed_after=settings.evidence_delayed_after_seconds),
             "window": window_counts(tracking_window, t, window_w, getattr(plan, "camera_interval_s", None) if plan else None, knowledge),
             "rejected_writes": rejections.get("tracking", {"count": 0})}
    if tracking_window:
        track["continuity"] = tracking_continuity(tracking_window, t, window_w, settings.distance_max_gap_seconds, settings.distance_min_confidence)
    perf = {"source": "performance"}
    if latest_perf is None:
        perf.update(status="unavailable" if performance is not None else "unknown", basis="no performance entry ends by the review time")
    else:
        age = (t - latest_perf.period_end).total_seconds()
        perf.update(status="present" if performance and performance.get("available") else "insufficient",
                    basis=("composite available" if performance and performance.get("available") else "; ".join((performance or {}).get("reasons") or ["composite unavailable"])),
                    last_period_end=iso(latest_perf.period_end), last_entered_at=iso(latest_perf.created_at), age_s=round(age, 3),
                    entry_delay_s=round((latest_perf.created_at - latest_perf.period_end).total_seconds(), 3))
    if performance is not None:
        perf.update(counted=len(performance.get("counted_observation_ids") or []), identity_uncertain_excluded=performance.get("identity_uncertain_excluded", 0),
                    evidence_sources=performance.get("sources", {}))
    used = {}
    route = ((review or {}).get("route") or {}).get("name")
    if review is None or not review.get("available", True) or route is None:
        used = {"note": "no experimental review was calculated for this athlete (no profile, or not on the field); nothing below was used for a recommendation"}
    else:
        used["physiological_index"] = ("not used: performance-only route (no wearable in the session plan)" if route == "performance_only"
                                       else "used" if (phys_index or {}).get("available") else "required but unavailable")
        used["performance_composite"] = "used" if (performance or {}).get("available") else "required but unavailable"
        used["tracking"] = "not an input of this rule version (shown for context only)"
    sources = {"wearable": wear, "tracking": track, "performance": perf}
    msgs = []
    for k, v in sources.items():
        st = v.get("status")
        if st in ("present", "not_used"):
            continue
        if k == "tracking" and st == "unknown":
            continue  # no camera evidence and none planned: not a problem to report first
        msgs.append(f"{LABELS[k]} {st}" + (f" ({v['basis']})" if v.get("basis") else ""))
    if recording_stopped:
        msgs.append("recording stopped: ages are measured at the review time, not now")
    return {"summary": msgs or ["all planned inputs present"], "sources": sources, "used_by_review": used,
            "knowledge": knowledge, "thresholds_note": "stale / disconnected / delayed labels use adjustable engineering defaults, not the team's freshness requirements"}
