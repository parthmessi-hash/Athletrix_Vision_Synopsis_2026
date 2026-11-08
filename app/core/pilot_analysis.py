"""Paired analysis of the fatigue pilot: saved pre-sprint scores against the independently timed sprint slowdown
(pure functions; scripts/evaluate.py pilot does the I/O).

ROWS: one per participant / session / round with a saved prediction (scores as they were at the cutoff) and, entered
later and separately, the timed 20 m sprint result. ELIGIBLE rows only: a prediction captured before the sprint
(prospective: the marker reached the engine within the tolerance of the stated sprint time) in a round the operator did
not mark invalid. Retrospective-only predictions (backdated markers) and invalid rounds are listed with their reason and
never enter a correlation; an invalid round's time is never the best reference.
UNITS: slowdown_pct = 100 x (time_s - best_s) / best_s, both in seconds from the independent timing (percent; 0 for the
best round, positive = slower). OUTCOME: percent slowdown = 100 x (time - best) / best, where best is
the participant's fastest valid timed sprint IN THAT SESSION. The best is a retrospective reference for the outcome only:
it never enters a score, a feature or any normalisation (scores are saved before it is known).
COMPARISON: for each candidate, coverage (rows with a value and an outcome) and the reasons for missing values; then the
MATCHED rows (all three candidates and the outcome present) with Pearson r per candidate, and the differences between
the combined r and each single-source r. Per participant and per session as well as pooled. Rounds of one person are
repeated observations: the pooled figure is not a count of independent participants. Round order is compared too (r of
round number with the outcome and with each score), so a score that simply rises with time is visible. Self-ratings are
a separate exploratory comparison and never a score input.
ENGINEERING TARGETS from the plan (not proof of effectiveness): combined r >= 0.5, and combined r higher than both
single-source r, on the matched rows. A target with an undefined correlation is "undefined", never "met".
"""

from __future__ import annotations

import math
import statistics

CANDIDATES = ("vest_only", "camera_only", "combined")
SMALL_N = 5


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


def not_eligible(r: dict) -> str | None:
    if r.get("invalid"):
        return "round marked invalid by the operator: " + (r.get("invalid_reason") or "no reason given")
    if r.get("prospective") is False:
        return "retrospective-only prediction (marker reached the engine after the stated sprint time): not a pre-sprint prediction"
    return None


def with_outcomes(rows: list[dict]) -> list[dict]:
    """Adds slowdown_pct (or outcome_missing) using the best valid time of the same participant and session."""
    best = {}
    for r in rows:
        if r.get("time_s") and not r.get("invalid"):
            k = (r["participant"], r["session"])
            best[k] = min(best.get(k, math.inf), r["time_s"])
    out = []
    for r in rows:
        k = (r["participant"], r["session"])
        if not r.get("time_s") or k not in best:
            out.append({**r, "slowdown_pct": None, "outcome_missing": r.get("time_missing_reason") or ("no valid round to compare with" if r.get("time_s") else "no timed result")})
        else:
            out.append({**r, "slowdown_pct": round(100 * (r["time_s"] - best[k]) / best[k], 4), "best_time_s": best[k]})
    return out


def compare(rows: list[dict]) -> dict:
    """rows: [{participant, session, round, time_s|None, time_missing_reason, scores{c: value|None}, reasons{c: [..]}, self_rating|None}]"""
    rows = with_outcomes(rows)
    ineligible = [{"participant": r["participant"], "session": r["session"], "round": r["round"], "reason": not_eligible(r)} for r in rows if not_eligible(r)]
    all_rows, rows = rows, [r for r in rows if not not_eligible(r)]
    coverage = {}
    for c in CANDIDATES:
        have = [r for r in rows if r["slowdown_pct"] is not None and r["scores"].get(c) is not None]
        why = {}
        for r in rows:
            if r["scores"].get(c) is None:
                for x in (r.get("reasons", {}).get(c) or ["no saved prediction"])[:1]:
                    why[x] = why.get(x, 0) + 1
            elif r["slowdown_pct"] is None:
                why[f"outcome missing: {r['outcome_missing']}"] = why.get(f"outcome missing: {r['outcome_missing']}", 0) + 1
        for x in ineligible:
            why[x["reason"]] = why.get(x["reason"], 0) + 1
        coverage[c] = {"rows": len(all_rows), "usable": len(have), "missing_reasons": why}
    matched = [r for r in rows if r["slowdown_pct"] is not None and all(r["scores"].get(c) is not None for c in CANDIDATES)]

    def block(sub: list[dict]) -> dict:
        y = [r["slowdown_pct"] for r in sub]
        res = {c: pearson([r["scores"][c] for r in sub], y) for c in CANDIDATES}
        comb = res["combined"]["r"]
        res["combined_minus_vest_only"] = None if comb is None or res["vest_only"]["r"] is None else round(comb - res["vest_only"]["r"], 4)
        res["combined_minus_camera_only"] = None if comb is None or res["camera_only"]["r"] is None else round(comb - res["camera_only"]["r"], 4)
        res["round_order"] = {"round_vs_slowdown": pearson([r["round"] for r in sub], y), **{f"round_vs_{c}": pearson([r["round"] for r in sub], [r["scores"][c] for r in sub]) for c in CANDIDATES}}
        res["rows"] = len(sub)
        return res
    pooled = block(matched)
    by_participant = {p: block([r for r in matched if r["participant"] == p]) for p in sorted({r["participant"] for r in matched})}
    by_session = {f"{p}/{s}": block([r for r in matched if (r["participant"], r["session"]) == (p, s)]) for p, s in sorted({(r["participant"], r["session"]) for r in matched})}
    rc, rv, rk = pooled["combined"]["r"], pooled["vest_only"]["r"], pooled["camera_only"]["r"]
    targets = {"combined_r_at_least_0.5": None if rc is None else rc >= 0.5,
               "combined_above_both_single_source": None if None in (rc, rv, rk) else (rc > rv and rc > rk),
               "note": "engineering targets from the plan, on the pooled matched rows; meeting them is not proof of effectiveness, and None means undefined"}
    rated = [r for r in rows if r.get("self_rating") is not None and r["slowdown_pct"] is not None]
    return {"rows": all_rows, "ineligible": ineligible, "units": "slowdown_pct = 100 x (time_s - best_s) / best_s (percent; seconds from the independent timing)",
            "coverage": coverage, "matched_rows": len(matched), "participants": len({r["participant"] for r in matched}),
            "sessions": len({(r["participant"], r["session"]) for r in matched}), "pooled": pooled, "by_participant": by_participant, "by_session": by_session,
            "targets": targets, "self_rating_exploratory": {"rows": len(rated), "self_rating_vs_slowdown": pearson([r["self_rating"] for r in rated], [r["slowdown_pct"] for r in rated]),
                                                            "note": "exploratory only; self-ratings are never a score input"},
            "note": "Rounds of one participant are repeated observations, not independent participants. The best sprint of a session is a retrospective "
                    "reference for the outcome only."}


def rows_from_package(pkg) -> list[dict]:
    """Rows from an exported recording package (format 8): the CURRENT sprint start's saved prediction and the current result."""
    from app.store.study import current  # noqa: PLC0415
    cur = current(pkg.study)
    sess = next((e for e in reversed(cur) if e.kind == "session"), None)
    code = {e.athlete_id: e.participant_code for e in pkg.study if e.kind == "wearer" and e.participant_code}
    preds = {e.prediction.get("sprint_start_id"): e for e in pkg.study if e.kind == "prediction"}
    out = []
    for s in [e for e in cur if e.kind == "sprint_start"]:
        res = next((e for e in cur if e.kind == "sprint_result" and e.athlete_id == s.athlete_id and e.round == s.round), None)
        rate = next((e for e in cur if e.kind == "self_rating" and e.athlete_id == s.athlete_id and e.round == s.round), None)
        p = preds.get(s.id)
        cands = ((p.prediction or {}).get("result") or {}).get("candidates") if p else None
        end = next((e for e in cur if e.kind == "round_end" and e.athlete_id == s.athlete_id and e.round == s.round), None)
        out.append({"invalid": bool(end and end.invalid), "invalid_reason": end.notes if end and end.invalid else None,
                    "prospective": (p.prediction or {}).get("prospective", True) if p else None, "marker_lag_s": (p.prediction or {}).get("marker_lag_s") if p else None,
                    "participant": code.get(s.athlete_id, s.athlete_id), "session": sess.session_label if sess else pkg.recording["id"], "recording_id": pkg.recording["id"],
                    "round": s.round, "cutoff": p.prediction["cutoff"] if p else None,
                    "scores": {c: (cands or {}).get(c, {}).get("value") for c in CANDIDATES},
                    "reasons": {c: (cands or {}).get(c, {}).get("reasons") or ([] if cands else ["no score: " + str(((p.prediction or {}).get("result") or {}).get("reason") if p else "no prediction")]) for c in CANDIDATES},
                    "profile_sha256": (((p.prediction or {}).get("result") or {}).get("profile") or {}).get("sha256") if p else None,
                    "time_s": res.time_s if res else None, "time_missing_reason": (res.missing_reason if res else "no result entered"),
                    "self_rating": rate.value if rate else None, "fictional": bool(sess and sess.fictional)})
    return sorted(out, key=lambda r: (r["participant"], r["session"], r["round"]))
