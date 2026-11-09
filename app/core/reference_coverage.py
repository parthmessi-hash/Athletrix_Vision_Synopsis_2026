"""Reference coverage for the EXPERIMENTAL movement-matched heart-rate feature (hr_speed_matched): a compact,
plain-language summary for the active wearer of which speed bands the player's own reference can be compared in, how
much recent movement falls in those bands, and what data was lost and why.

It is built ONLY from the feature result the score already produced (its bands, pair counts, matched share, cause and
accounting) and the profile's own thresholds. It makes no second decision: "comparable" is exactly the score's matched
band (both windows at least min_pairs_per_band), and the summary's availability is the feature's. It does not prescribe
an exercise protocol; the baseline procedure is the team's decision before an approved pilot.
"""

from __future__ import annotations

from app.core.fatigue import LOSS_LABELS, matched_data_loss

CAUSE_HEADLINE = {
    "covered": "The reference covers the recent movement: heart rate is compared at similar movement.",
    "not_comparable": "The reference does not include this kind of movement, so heart rate is not compared (not read as fatigue or freshness).",
    "too_few_recent": "Too few recent heart-rate samples with a movement context to compare.",
    "missing_heart_rate": "Heart rate is missing in part of a window.",
    "camera_identity_uncertain": "The camera tracking for this player is uncertain (low confidence or predicted).",
    "camera_gap": "The camera lost this player for part of a window.",
    "near_test_sprint": "Most samples are next to a timed test sprint, which is left out by design.",
    "clock_alignment": "Heart rate and camera speed cannot be paired: the camera clock alignment is missing or too uncertain.",
    "reference_incomplete": "The player's reference is not complete yet (or the recent window still overlaps it).",
    "wearer_or_baseline": "This player has no usable wearer assignment, baseline or maximum heart rate at this time.",
}

NEXT = {
    "covered": "Nothing to fix. Bands listed as not comparable are simply left out of the comparison.",
    "not_comparable": ("Look at the bands marked 'reference lacks this range': the player's reference period did not include that speed. "
                       "Whether the reference period should include it is a baseline-procedure decision for the team before the pilot; "
                       "until then the estimate says 'not enough data' instead of guessing."),
    "too_few_recent": "Wait for more recent data, or check that both streams are arriving for this player.",
    "missing_heart_rate": "Check the strap contact and the wearable connection during the window named above; the camera side is not the cause.",
    "camera_identity_uncertain": "Check the camera view and this player's track identity in that period (occlusion, a crossing player, a lost track).",
    "camera_gap": "Check that the player stayed in the camera view and that the camera feed kept running in that period.",
    "near_test_sprint": "Expected after a timed sprint: wait until the excluded period has passed.",
    "clock_alignment": "Check the session's stated camera clock alignment and its uncertainty against the profile's tolerance.",
    "reference_incomplete": "Wait until the reference window has ended and the recent window no longer overlaps it; check which round start opened it.",
    "wearer_or_baseline": "Check the wearer assignment: the participant, their own accepted baseline and their maximum heart rate with its origin.",
}


def _band_note(b: dict, need: int) -> tuple[str, str]:
    if "excess_bpm" in b:
        return "comparable", f"comparable ({b['reference_pairs']} reference, {b['recent_pairs']} recent pairs)"
    if b["recent_pairs"] == 0:
        return "no_recent_movement", f"no recent movement here (reference {b['reference_pairs']} pairs)"
    if b["reference_pairs"] == 0:
        return "reference_lacks_range", f"reference lacks this range (0 reference pairs, {b['recent_pairs']} recent)"
    if b["reference_pairs"] < need:
        return "reference_too_sparse", f"reference has only {b['reference_pairs']} pairs here (needs {need}); {b['recent_pairs']} recent"
    return "too_few_recent", f"only {b['recent_pairs']} recent pairs here (needs {need})"


def reference_coverage(result: dict, prof: dict) -> dict | None:
    """The summary for one score result, or None when the profile does not use hr_speed_matched."""
    feat = (result.get("features") or {}).get("hr_speed_matched")
    spec = prof["features"].get("hr_speed_matched")
    if feat is None or spec is None:
        return None
    need = spec["min_pairs_per_band"]
    cause = "covered" if feat.get("available") else (feat.get("cause") or "not_comparable")
    out = {"caution": None, "athlete_id": result.get("athlete_id"), "at": result.get("at"), "feature_available": bool(feat.get("available")),
           "status": cause, "headline": CAUSE_HEADLINE.get(cause, cause), "reason": None if feat.get("available") else feat.get("reason"),
           "next_check": NEXT.get(cause, ""), "reference_window": feat.get("reference_window"), "recent_window": feat.get("window"),
           "thresholds": {"min_pairs_per_band": need, "min_matched_share": spec["min_matched_share"], "context_s": spec["context_s"],
                          "context_min_coverage": spec["context_min_coverage"], "band_edges_mps": spec["band_edges_mps"],
                          "alignment_tolerance_s": spec["alignment_tolerance_s"]},
           "experimental": True,
           "note": "Built from the score's own counts; it does not decide anything. Experimental; not a fatigue measurement."}
    if "bands" not in feat:
        out.update(bands=[], recent_share_in_comparable_bands=None, lost=None)
        return out
    bands = []
    for b in feat["bands"]:
        st, note = _band_note(b, need)
        bands.append({"band_mps": b["band_mps"], "label": f"{b['band_mps'][0]:g}-{b['band_mps'][1]:g} m/s", "reference_pairs": b["reference_pairs"],
                      "recent_pairs": b["recent_pairs"], "status": st, "note": note})
    acc = feat.get("accounting") or {}
    lost = {}
    for w in ("reference", "recent"):
        a = acc.get(w)
        if a:
            lost[w] = {"heart_rate_samples": a["heart_rate_samples"], "heart_rate_covered_s": a["heart_rate_covered_s"], "window_s": a["window_s"],
                       "paired": a["paired"], "lost": [{"why": LOSS_LABELS[k], "code": k, "samples": v} for k, v in a["lost"].items() if v]}
    caution = None
    if feat.get("available") and acc.get("reference") and acc.get("recent"):
        # available, but on fewer samples than the windows held: say what was lost (the value stands as scored)
        _, caution = matched_data_loss(spec, acc["reference"], acc["recent"])
    out.update(bands=bands, recent_share_in_comparable_bands=feat.get("matched_share_of_recent"), lost=lost,
               pairs=feat.get("pairs"), caution=caution)
    return out


def coverage_lines(cov: dict | None) -> list[str]:
    """Plain text for the operator CLI and reports."""
    if cov is None:
        return ["Reference coverage: this session's profile does not use the movement-matched heart-rate feature (experimental profile only)."]
    L = [f"Reference coverage (EXPERIMENTAL) for {cov['athlete_id']} at {cov['at']}: {cov['status'].upper()}", f"  {cov['headline']}"]
    if cov.get("recent_share_in_comparable_bands") is not None:
        L.append(f"  recent samples in comparable bands: {cov['recent_share_in_comparable_bands']:.0%} (the score needs {cov['thresholds']['min_matched_share']:.0%})")
    for b in cov["bands"]:
        L.append(f"  {b['label']:>12}: {b['note']}")
    for w, a in (cov.get("lost") or {}).items():
        parts = ", ".join(f"{x['samples']} {x['why']}" for x in a["lost"]) or "nothing lost"
        L.append(f"  {w}: heart rate {a['heart_rate_samples']} samples ({a['heart_rate_covered_s']:g} of {a['window_s']:g} s covered), {a['paired']} paired; lost: {parts}")
    if cov.get("reason"):
        L.append(f"  why: {cov['reason']}")
    if cov.get("caution"):
        L.append(f"  caution (the estimate stands, on fewer samples): {cov['caution']}")
    L.append(f"  next: {cov['next_check']}")
    return L
