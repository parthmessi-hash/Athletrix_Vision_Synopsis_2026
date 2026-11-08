"""Paired analysis of the fatigue pilot: saved pre-sprint scores against the independently timed 20 m sprint
(pure functions; scripts/evaluate.py pilot does the I/O).

OUTCOME: time_increase_pct = 100 x (time_s - best_s) / best_s, the PERCENT INCREASE IN SPRINT TIME relative to the best
valid time of the same participant in the same session (seconds from the independent timing; 0 for the best attempt,
positive = slower). It is not a percent loss of speed.
REFERENCE ATTEMPTS (which attempts may supply best_s): timed results (not missing) in rounds not marked invalid, by a
timing method the study manifest allows for the reference. Defined independently of the scores: a round whose score is
unavailable or whose prediction is retrospective still supplies its time, so the reference never moves because a
candidate was missing. The report names the round and result entry that supplied each best time.
PREDICTION ELIGIBILITY (which rows enter the comparison): rounds not marked invalid whose timing evidence meets the
declared policy (app/core/timing_evidence.py): confirmed_sequence (saved and confirmed before the operator's start
marker, or before an independently recorded start) or independent_start_reference (only the latter).
COMPARISON: the PRIMARY figures use the same eligible rows for all three candidates (all three scores and the outcome
present). Pearson r per candidate, per participant, per participant-session and pooled; rounds of one person are repeated
observations, not independent participants. Round order is compared too. An EXPLORATORY figure that adds the
timing-unverified rounds is shown separately and never replaces the primary one. Self-ratings are exploratory only.
ENGINEERING TARGETS (not proof): combined r >= 0.5 and above both single-source r on the primary rows.
"""

from __future__ import annotations

import math
import statistics

from app.core.timing_evidence import classify, eligible

CANDIDATES = ("vest_only", "camera_only", "combined")
SMALL_N = 5
OUTCOME = "time_increase_pct = 100 x (time_s - best_s) / best_s: percent increase in sprint time relative to the best valid time (not percent loss of speed)"


def pearson(xs: list[float], ys: list[float]) -> dict:
    n = len(xs)
    if n < 3:
        return {"r": None, "n": n, "why": "not enough data (fewer than 3 usable rounds)"}
    if len(set(xs)) == 1 or len(set(ys)) == 1:
        return {"r": None, "n": n, "why": "undefined: constant input"}
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx, syy = sum((x - mx) ** 2 for x in xs), sum((y - my) ** 2 for y in ys)
    r = sxy / math.sqrt(sxx * syy)
    return {"r": round(r, 4), "n": n, **({"flag": f"small sample (n < {SMALL_N})"} if n < SMALL_N else {})}


def timing_of(r: dict) -> dict:
    """Rows from older exports or fixtures without timing evidence: derived from the legacy flag, never promoted."""
    if r.get("timing"):
        return r["timing"]
    if r.get("prospective") is False:
        return {"category": "retrospective", "basis": "backdated_marker", "detail": "retrospective-only prediction (backdated marker)"}
    return {"category": "order_unverified", "basis": "marker_only", "detail": "legacy row: calculated at the start marker; order not verified"}


def references(rows: list[dict], methods: list[str] | None) -> dict:
    best: dict[tuple, dict] = {}
    for r in rows:
        if not r.get("time_s") or r.get("invalid") or (methods and r.get("method") not in methods):
            continue
        k = (r["participant"], r["session"])
        if k not in best or r["time_s"] < best[k]["time_s"]:
            best[k] = {"round": r["round"], "time_s": r["time_s"], "result_id": r.get("result_id"), "method": r.get("method"),
                       "corrected_from": [h["time_s"] for h in r.get("result_history", [])]}
    return best


def compare(rows: list[dict], policy: str = "confirmed_sequence", reference_methods: list[str] | None = None) -> dict:
    """rows: [{participant, session, round, time_s|None, time_missing_reason, method, scores{c}, reasons{c}, timing, invalid,
    self_rating, prediction_id, result_id, result_history, ...}]"""
    best = references(rows, reference_methods)
    all_rows = []
    for r in rows:
        k = (r["participant"], r["session"])
        ev = timing_of(r)
        out = {**r, "timing": ev, "timing_meets_policy": eligible(ev, policy)}
        if not r.get("time_s"):
            out.update(time_increase_pct=None, outcome_missing=r.get("time_missing_reason") or "no timed result")
        elif r.get("invalid"):
            out.update(time_increase_pct=None, outcome_missing="round marked invalid")
        elif k not in best:
            out.update(time_increase_pct=None, outcome_missing="no valid reference attempt in this session")
        else:
            out.update(time_increase_pct=round(100 * (r["time_s"] - best[k]["time_s"]) / best[k]["time_s"], 4), best_reference_round=best[k]["round"])
        all_rows.append(out)

    def why_not(r):
        if r.get("invalid"):
            return "round marked invalid by the operator: " + (r.get("invalid_reason") or "no reason given")
        if not r["timing_meets_policy"]:
            return f"timing evidence {r['timing']['category']} ({r['timing']['basis']}) does not meet the declared policy {policy}"
        return None
    ineligible = [{"participant": r["participant"], "session": r["session"], "round": r["round"], "reason": why_not(r), "prediction_id": r.get("prediction_id")} for r in all_rows if why_not(r)]
    ok = [r for r in all_rows if not why_not(r)]
    coverage = {}
    for c in CANDIDATES:
        why = {}
        for r in all_rows:
            reason = why_not(r) or (None if r["scores"].get(c) is not None else ((r.get("reasons", {}).get(c) or ["no saved prediction"])[0])) or \
                (None if r["time_increase_pct"] is not None else f"outcome missing: {r['outcome_missing']}")
            if reason:
                why[reason] = why.get(reason, 0) + 1
        usable = sum(1 for r in ok if r["scores"].get(c) is not None and r["time_increase_pct"] is not None)
        coverage[c] = {"rows": len(all_rows), "usable": usable, "excluded": len(all_rows) - usable, "excluded_reasons": why}
    matched = [r for r in ok if r["time_increase_pct"] is not None and all(r["scores"].get(c) is not None for c in CANDIDATES)]

    def block(sub: list[dict]) -> dict:
        y = [r["time_increase_pct"] for r in sub]
        res = {c: pearson([r["scores"][c] for r in sub], y) for c in CANDIDATES}
        comb = res["combined"]["r"]
        res["combined_minus_vest_only"] = None if comb is None or res["vest_only"]["r"] is None else round(comb - res["vest_only"]["r"], 4)
        res["combined_minus_camera_only"] = None if comb is None or res["camera_only"]["r"] is None else round(comb - res["camera_only"]["r"], 4)
        res["round_order"] = {"round_vs_time_increase": pearson([r["round"] for r in sub], y),
                              **{f"round_vs_{c}": pearson([r["round"] for r in sub], [r["scores"][c] for r in sub]) for c in CANDIDATES}}
        res["rows"] = len(sub)
        return res
    pooled = block(matched)
    by_participant = {p: block([r for r in matched if r["participant"] == p]) for p in sorted({r["participant"] for r in matched})}
    by_session = {f"{p}/{s}": block([r for r in matched if (r["participant"], r["session"]) == (p, s)]) for p, s in sorted({(r["participant"], r["session"]) for r in matched})}
    rc, rv, rk = pooled["combined"]["r"], pooled["vest_only"]["r"], pooled["camera_only"]["r"]
    targets = {"combined_r_at_least_0.5": None if rc is None else rc >= 0.5,
               "combined_above_both_single_source": None if None in (rc, rv, rk) else (rc > rv and rc > rk),
               "note": "engineering targets from the plan, on the primary (policy-eligible, matched) rows; meeting them is not proof of effectiveness; None means undefined"}
    explore = [r for r in all_rows if not r.get("invalid") and r["timing"]["category"] != "retrospective" and r["time_increase_pct"] is not None
               and all(r["scores"].get(c) is not None for c in CANDIDATES)]
    counts: dict[str, int] = {}
    for r in all_rows:
        key = f"{r['timing']['category']} / {r['timing']['basis']}"
        counts[key] = counts.get(key, 0) + 1
    rated = [r for r in ok if r.get("self_rating") is not None and r["time_increase_pct"] is not None]
    return {"rows": all_rows, "ineligible": ineligible, "outcome": OUTCOME, "units": OUTCOME, "policy": policy, "reference_methods": reference_methods or "all timed methods",
            "best_references": {f"{p}/{s}": v for (p, s), v in sorted(best.items())}, "timing_evidence_counts": counts,
            "coverage": coverage, "matched_rows": len(matched), "participants": len({r["participant"] for r in matched}),
            "sessions": len({(r["participant"], r["session"]) for r in matched}), "pooled": pooled, "by_participant": by_participant, "by_session": by_session,
            "targets": targets,
            "exploratory_including_unverified_timing": {"rows": len(explore), **{c: pearson([r["scores"][c] for r in explore], [r["time_increase_pct"] for r in explore]) for c in CANDIDATES},
                                                        "note": "EXPLORATORY: adds rounds whose save-before-start order is unverified; never the primary result"},
            "self_rating_exploratory": {"rows": len(rated), "self_rating_vs_time_increase": pearson([r["self_rating"] for r in rated], [r["time_increase_pct"] for r in rated]),
                                        "note": "exploratory only; self-ratings are never a score input"},
            "note": "Rounds of one participant are repeated observations, not independent participants. The best valid time is a retrospective reference for the outcome only."}


def rows_from_package(pkg) -> list[dict]:
    """Rows from an exported recording package (format 8), one per wearer and round that has a saved score or a start."""
    from app.store.study import current  # noqa: PLC0415
    cur = current(pkg.study)
    by_id = {e.id: e for e in pkg.study}
    sess = next((e for e in reversed(cur) if e.kind == "session"), None)
    code = {e.athlete_id: e.participant_code for e in pkg.study if e.kind == "wearer" and e.participant_code}
    preds_by_start = {e.prediction.get("sprint_start_id"): e for e in pkg.study if e.kind == "prediction" and e.prediction.get("sprint_start_id")}
    keys = sorted({(e.athlete_id, e.round) for e in cur if e.kind in ("sprint_start", "prediction") and e.round is not None})
    out = []
    for aid, rnd in keys:
        start = next((e for e in cur if e.kind == "sprint_start" and (e.athlete_id, e.round) == (aid, rnd)), None)
        if start and start.confirms_prediction_id:
            p = by_id.get(start.confirms_prediction_id)
        elif start:
            p = preds_by_start.get(start.id)
        else:   # saved, never started
            p = next((e for e in reversed(cur) if e.kind == "prediction" and (e.athlete_id, e.round) == (aid, rnd)), None)
        res = next((e for e in cur if e.kind == "sprint_result" and (e.athlete_id, e.round) == (aid, rnd)), None)
        history, x = [], res
        while x is not None and x.supersedes:
            x = by_id.get(x.supersedes)
            if x is not None:
                history.append({"id": x.id, "time_s": x.time_s, "missing_reason": x.missing_reason, "corrected_by_reason": None})
        rate = next((e for e in cur if e.kind == "self_rating" and (e.athlete_id, e.round) == (aid, rnd)), None)
        end = next((e for e in cur if e.kind == "round_end" and (e.athlete_id, e.round) == (aid, rnd)), None)
        result = ((p.prediction or {}).get("result") or {}) if p else {}
        cands = result.get("candidates")
        ev = classify(p.model_dump(mode="json"), start.model_dump(mode="json") if start else None, res.model_dump(mode="json") if res else None) if p else \
            {"category": "retrospective", "basis": "no_prediction", "detail": "no saved prediction"}
        out.append({"participant": code.get(aid, aid), "session": sess.session_label if sess else pkg.recording["id"], "recording_id": pkg.recording["id"], "round": rnd,
                    "invalid": bool(end and end.invalid), "invalid_reason": end.notes if end and end.invalid else None, "invalidation_id": end.id if end and end.invalid else None,
                    "prediction_id": p.id if p else None, "sprint_start_id": start.id if start else None, "cutoff": (p.prediction or {}).get("cutoff") if p else None,
                    "rules_version": (p.prediction or {}).get("rules_version", 1) if p else None, "timing": ev,
                    "scores": {c: (cands or {}).get(c, {}).get("value") for c in CANDIDATES},
                    "reasons": {c: (cands or {}).get(c, {}).get("reasons") or ([] if cands else ["no score: " + str(result.get("reason") or "no prediction")]) for c in CANDIDATES},
                    "profile_sha256": (result.get("profile") or {}).get("sha256"),
                    "result_id": res.id if res else None, "result_history": history, "method": res.method if res else None,
                    "time_s": res.time_s if res else None, "time_missing_reason": (res.missing_reason if res else "no result entered"),
                    "self_rating": rate.value if rate else None, "fictional": bool(sess and sess.fictional)})
    return out
