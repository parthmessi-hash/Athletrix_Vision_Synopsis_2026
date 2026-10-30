"""Individual-performance review: evidence per area, and a composite only when a rubric and enough evidence exist.

Per athlete and recording, the review lists every observation and, per area: assessed / unable-to-assess (with
reasons) / no-opportunity counts, sources, and the outcome totals (pass attempts and completions; possessions and
turnovers; followed / partially followed / not followed). Nothing here reads physiology, fatigue, distance or coach
context evidence (leadership, chemistry, pressure situations).

SCORING (only with a configured rubric, PERFORMANCE_RUBRIC_PATH). Each rubric measure turns a rate into 0-100 between
the rubric's floor (0) and ceiling (100); a rate outside that band maps to 0 or 100 (the rubric's own bounds, stated
in the response):
  pass_completion        completions / attempts over assessed pass observations; opportunities = attempts
  possession_retention   1 - turnovers / possessions over assessed turnover observations; opportunities = possessions
  instruction_followed   (followed + partial_credit x partially_followed) / assessed tactical observations of THAT area;
                         opportunities = those observations
A measure with fewer opportunities than its min_opportunities is unavailable. An area score is the weighted mean of
its measures and is unavailable if any measure is. The composite is the weighted mean of the four area scores and is
unavailable unless all four are available. Unable-to-assess and no-opportunity observations never count as zeros.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from pathlib import Path

from app.models.performance import AREAS, PerformanceObservation, PerformanceRubric

MEASURE_AREAS = {"pass_completion": ("execution",), "possession_retention": ("execution",),
                 "instruction_followed": ("decision_making", "movement_positioning", "coach_strategy")}


class RubricError(Exception):
    pass


def load_rubric(path: str | Path | None) -> tuple[PerformanceRubric | None, str | None]:
    """(rubric, sha256) or (None, None) when no path is configured; a configured but invalid file raises RubricError."""
    if not path:
        return None, None
    data = Path(path).read_bytes()
    try:
        rubric = PerformanceRubric.model_validate_json(data)
    except Exception as exc:  # noqa: BLE001
        raise RubricError(f"the performance rubric {path} is invalid: {exc}") from exc
    for area, spec in rubric.areas.items():
        for m in spec.measures:
            if area not in MEASURE_AREAS[m.measure]:
                raise RubricError(f"measure {m.measure} cannot score area {area}")
    return rubric, hashlib.sha256(data).hexdigest()


def _area_evidence(obs: list[PerformanceObservation], area: str) -> dict:
    mine = [o for o in obs if o.area == area]
    assessed = [o for o in mine if o.assessment == "assessed"]
    ev = {"observations": len(mine), "assessed": len(assessed),
          "unable_to_assess": len([o for o in mine if o.assessment == "unable_to_assess"]),
          "unable_reasons": dict(Counter(o.unable_reason for o in mine if o.unable_reason)),
          "no_opportunity": len([o for o in mine if o.assessment == "no_opportunity"]),
          "sources": dict(Counter(o.evidence_source for o in mine)),
          "reference_labels": len([o for o in mine if o.reference_label])}
    if area == "execution":
        passes = [o for o in assessed if o.kind == "pass"]
        tos = [o for o in assessed if o.kind == "turnover"]
        ev["passes"] = {"attempts": sum(o.attempts for o in passes), "completions": sum(o.completions for o in passes), "observations": len(passes)}
        ev["possessions"] = {"possessions": sum(o.possessions for o in tos), "turnovers": sum(o.turnovers for o in tos), "observations": len(tos)}
    else:
        tac = [o for o in assessed if o.kind == "tactical_observation"]
        ev["tactical"] = dict(Counter(o.outcome for o in tac)) | {"observations": len(tac)}
    return ev


def _measure(ev: dict, m) -> dict:
    if m.measure == "pass_completion":
        n, rate = ev["passes"]["attempts"], (ev["passes"]["completions"] / ev["passes"]["attempts"] if ev["passes"]["attempts"] else None)
    elif m.measure == "possession_retention":
        n, rate = ev["possessions"]["possessions"], (1 - ev["possessions"]["turnovers"] / ev["possessions"]["possessions"] if ev["possessions"]["possessions"] else None)
    else:
        t = ev["tactical"]
        n = t["observations"]
        rate = (t.get("followed", 0) + m.partial_credit * t.get("partially_followed", 0)) / n if n else None
    if n < m.min_opportunities or rate is None:
        return {"measure": m.measure, "available": False, "opportunities": n, "reason": f"{n} opportunities, fewer than the rubric's minimum of {m.min_opportunities}"}
    score = 100 * min(1.0, max(0.0, (rate - m.floor) / (m.ceiling - m.floor)))
    return {"measure": m.measure, "available": True, "opportunities": n, "rate": round(rate, 4), "score": round(score, 2), "weight": m.weight}


def review(observations: list[PerformanceObservation], rubric: PerformanceRubric | None, rubric_sha: str | None) -> dict:
    areas = {a: {"evidence": _area_evidence(observations, a)} for a in AREAS}
    reasons = []
    if rubric is None:
        reasons.append("no performance rubric is configured (PERFORMANCE_RUBRIC_PATH): the team has not set scoring weights, so no area score or composite is calculated")
        for a in AREAS:
            areas[a]["score"] = None
    else:
        for a in AREAS:
            spec = rubric.areas.get(a)
            if spec is None:
                areas[a]["score"] = None
                areas[a]["unavailable_reason"] = "the rubric has no settings for this area"
                reasons.append(f"{a}: no rubric settings")
                continue
            measures = [_measure(areas[a]["evidence"], m) for m in spec.measures]
            areas[a]["measures"] = measures
            if all(x["available"] for x in measures):
                w = sum(x["weight"] for x in measures)
                areas[a]["score"] = round(sum(x["score"] * x["weight"] for x in measures) / w, 2)
            else:
                areas[a]["score"] = None
                areas[a]["unavailable_reason"] = "; ".join(f"{x['measure']}: {x['reason']}" for x in measures if not x["available"])
                reasons.append(f"{a}: {areas[a]['unavailable_reason']}")
    composite = None
    if rubric is not None and all(areas[a]["score"] is not None for a in AREAS):
        w = sum(rubric.areas[a].weight for a in AREAS)
        composite = round(sum(areas[a]["score"] * rubric.areas[a].weight for a in AREAS) / w, 2)
    return {"areas": areas,
            "rubric": None if rubric is None else {"version": rubric.version, "sha256": rubric_sha, "fictional": rubric.fictional, "note": rubric.note},
            "composite": {"available": composite is not None, "value": composite, "fictional_rubric": bool(rubric and rubric.fictional),
                          "reasons": reasons if composite is None else []},
            "observations": [o.model_dump(mode="json") for o in sorted(observations, key=lambda o: (o.period_start, o.created_at, o.id))],
            "rules": "Experimental evidence summary. A composite needs a configured rubric and enough assessed evidence in all four areas; "
                     "unable-to-assess and no-opportunity observations never count as zeros. Not derived from fatigue, distance, preset ability "
                     "or coach-context evidence (leadership, chemistry, pressure situations). Manual entries are human annotations, not automatic recognition."}
