"""Individual-performance review: evidence per area, and a composite only when a rubric and enough evidence exist.

Per athlete and recording, the review lists every observation and, per area: assessed / unable-to-assess (with
reasons) / no-opportunity counts, sources, and the outcome totals (pass attempts and completions; possessions and
turnovers; followed / partially followed / not followed). Nothing here reads physiology, fatigue, distance or coach
context evidence (leadership, chemistry, pressure situations).

WHICH OBSERVATIONS COUNT (app/models/performance.py): a superseded observation never counts (its correction does); with a
review time `at`, only observations whose period ended at or before it, and with a window only those that started within
it; legacy (version-1) tactical observations under decision_making or movement_positioning, uncertain-identity
observations and unresolved overlapping observations of the same kind are listed with their status and not scored.
Observations are entered by people after the fact, so a review "at" a past instant uses evidence entered later about
periods that had ended by then; the response counts those.

SCORING (only with a configured rubric, PERFORMANCE_RUBRIC_PATH, schema_version 2). Each rubric measure turns a rate
into 0-100 between the rubric's floor (0) and ceiling (100); a rate outside that band maps to 0 or 100 (the rubric's own
bounds, stated in the response):
  pass_completion        execution: completions / attempts over counted pass observations; opportunities = attempts
  possession_retention   execution: 1 - turnovers / possessions over counted turnover observations; opportunities = possessions
  decision_quality       decision_making: (good + partial_credit x acceptable) / opportunities over counted decision observations
  positioning_quality    movement_positioning: the same over counted positioning observations
  instruction_followed   coach_strategy: (followed + partial_credit x partially_followed) / counted tactical observations
A measure with fewer opportunities than its min_opportunities is unavailable. An area score is the weighted mean of
its measures and is unavailable if any measure is. The composite is the weighted mean of the four area scores and is
unavailable unless all four are available. Unable-to-assess and no-opportunity observations never count as zeros.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from app.models.performance import AREAS, PerformanceObservation, PerformanceRubric, is_legacy

MEASURE_AREAS = {"pass_completion": ("execution",), "possession_retention": ("execution",), "decision_quality": ("decision_making",),
                 "positioning_quality": ("movement_positioning",), "instruction_followed": ("coach_strategy",)}
JUDGED_KIND = {"decision_quality": "decision", "positioning_quality": "positioning"}


class RubricError(Exception):
    pass


def parse_rubric(data: bytes, where: str = "the performance rubric") -> tuple[PerformanceRubric, str]:
    """(rubric, sha256 of the exact bytes); RubricError when invalid."""
    try:
        rubric = PerformanceRubric.model_validate_json(data)
    except Exception as exc:  # noqa: BLE001
        raise RubricError(f"{where} is invalid: {exc}") from exc
    for area, spec in rubric.areas.items():
        for m in spec.measures:
            if area not in MEASURE_AREAS[m.measure]:
                raise RubricError(f"measure {m.measure} cannot score area {area}")
    return rubric, hashlib.sha256(data).hexdigest()


def load_rubric(path: str | Path | None) -> tuple[PerformanceRubric | None, str | None]:
    """(rubric, sha256) or (None, None) when no path is configured; a configured but invalid file raises RubricError."""
    if not path:
        return None, None
    return parse_rubric(Path(path).read_bytes(), f"the performance rubric {path}")


def classify(observations: list[PerformanceObservation], at: datetime | None = None, window_s: float | None = None) -> dict[str, str]:
    """observation id -> status: counted | unable_to_assess | no_opportunity | superseded | outside_review_time |
    legacy_unscored | identity_uncertain | potential_duplicate (see the module docstring)."""
    superseded = {o.supersedes for o in observations if o.supersedes}
    status: dict[str, str] = {}
    for o in observations:
        if o.id in superseded:
            status[o.id] = "superseded"
        elif at is not None and (o.period_end > at or (window_s is not None and o.period_start < at - timedelta(seconds=window_s))):
            status[o.id] = "outside_review_time"
        elif is_legacy(o.kind, o.area):
            status[o.id] = "legacy_unscored"
        elif o.assessment != "assessed":
            status[o.id] = o.assessment
        elif o.identity == "uncertain":
            status[o.id] = "identity_uncertain"
        else:
            status[o.id] = "counted"
    live = [o for o in observations if status[o.id] == "counted"]
    for i, a in enumerate(live):
        for b in live[i + 1:]:
            if a.athlete_id == b.athlete_id and a.kind == b.kind and a.period_start < b.period_end and b.period_start < a.period_end \
                    and b.id not in a.distinct_from and a.id not in b.distinct_from:
                status[a.id] = status[b.id] = "potential_duplicate"
    return status


def _area_evidence(obs: list[PerformanceObservation], status: dict[str, str], area: str) -> dict:
    mine = [o for o in obs if o.area == area]
    counted = [o for o in mine if status[o.id] == "counted"]
    ev = {"observations": len(mine), "counted": len(counted), "status": dict(Counter(status[o.id] for o in mine)),
          "unable_reasons": dict(Counter(o.unable_reason for o in mine if o.unable_reason and status[o.id] == "unable_to_assess")),
          "sources": dict(Counter(o.evidence_source for o in counted)), "reference_labels": len([o for o in counted if o.reference_label])}
    if area == "execution":
        passes = [o for o in counted if o.kind == "pass"]
        tos = [o for o in counted if o.kind == "turnover"]
        ev["passes"] = {"attempts": sum(o.attempts for o in passes), "completions": sum(o.completions for o in passes), "observations": len(passes)}
        ev["possessions"] = {"possessions": sum(o.possessions for o in tos), "turnovers": sum(o.turnovers for o in tos), "observations": len(tos)}
    elif area == "coach_strategy":
        tac = [o for o in counted if o.kind == "tactical_observation"]
        ev["tactical"] = dict(Counter(o.outcome for o in tac)) | {"observations": len(tac)}
    else:
        judged = [o for o in counted if o.kind in ("decision", "positioning")]
        ev["judged"] = {"opportunities": sum(o.opportunities for o in judged), "good": sum(o.rated_good for o in judged), "acceptable": sum(o.rated_acceptable for o in judged),
                        "poor": sum(o.rated_poor for o in judged), "observations": len(judged)}
    return ev


def _measure(ev: dict, m) -> dict:
    if m.measure == "pass_completion":
        n, rate = ev["passes"]["attempts"], (ev["passes"]["completions"] / ev["passes"]["attempts"] if ev["passes"]["attempts"] else None)
    elif m.measure == "possession_retention":
        n, rate = ev["possessions"]["possessions"], (1 - ev["possessions"]["turnovers"] / ev["possessions"]["possessions"] if ev["possessions"]["possessions"] else None)
    elif m.measure in JUDGED_KIND:
        j = ev["judged"]
        n = j["opportunities"]
        rate = (j["good"] + m.partial_credit * j["acceptable"]) / n if n else None
    else:
        t = ev["tactical"]
        n = t["observations"]
        rate = (t.get("followed", 0) + m.partial_credit * t.get("partially_followed", 0)) / n if n else None
    if n < m.min_opportunities or rate is None:
        return {"measure": m.measure, "available": False, "opportunities": n, "reason": f"{n} opportunities, fewer than the rubric's minimum of {m.min_opportunities}"}
    score = 100 * min(1.0, max(0.0, (rate - m.floor) / (m.ceiling - m.floor)))
    return {"measure": m.measure, "available": True, "opportunities": n, "rate": round(rate, 4), "score": round(score, 2), "weight": m.weight}


def _review_time(observations, status, at, window_s, knowledge) -> dict:
    later = len([o for o in observations if status[o.id] == "counted" and o.created_at > at])
    return {"at": at.isoformat().replace("+00:00", "Z"), "window_s": window_s, "knowledge": knowledge, "entered_after_review_time": later,
            "label": "as entered: only observations entered by the review time" if knowledge == "as_entered" else
                     (f"retrospective: {later} counted observations were entered after the review time; not what could have been shown live" if later
                      else "every counted observation had been entered by the review time")}


def review(observations: list[PerformanceObservation], rubric: PerformanceRubric | None, rubric_sha: str | None,
           at: datetime | None = None, window_s: float | None = None, knowledge: str = "retrospective") -> dict:
    """With `at` and knowledge "as_entered", only observations entered by `at` exist for the review (app/core/knowledge.py):
    a correction entered later is ignored and the observation it replaced counts, as it would have then."""
    if at is not None and knowledge == "as_entered":
        observations = [o for o in observations if o.created_at <= at]
    status = classify(observations, at, window_s)
    areas = {a: {"evidence": _area_evidence(observations, status, a)} for a in AREAS}
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
    superseded_by = {o.supersedes: o.id for o in observations if o.supersedes}
    flags = Counter(status.values())
    if flags.get("potential_duplicate"):
        reasons.append(f"{flags['potential_duplicate']} overlapping observations of the same kind were left out as potential duplicates (mark them distinct_from each other, or correct one)")
    if flags.get("identity_uncertain"):
        reasons.append(f"{flags['identity_uncertain']} observations with uncertain player identity were left out")
    counted_ids = sorted(i for i, st in status.items() if st == "counted")
    return {"areas": areas,
            "rubric": None if rubric is None else {"version": rubric.version, "sha256": rubric_sha, "fictional": rubric.fictional, "note": rubric.note},
            "composite": {"available": composite is not None, "value": composite, "fictional_rubric": bool(rubric and rubric.fictional),
                          "reasons": reasons if composite is None else [r for r in reasons if "left out" in r]},
            "review_time": None if at is None else _review_time(observations, status, at, window_s, knowledge),
            "counted_observation_ids": counted_ids,
            "observations": [o.model_dump(mode="json") | {"status": status[o.id], "superseded_by": superseded_by.get(o.id)}
                             for o in sorted(observations, key=lambda o: (o.period_start, o.created_at, o.id))],
            "rules": "Experimental evidence summary. A composite needs a configured rubric and enough counted evidence in all four areas; "
                     "unable-to-assess and no-opportunity observations never count as zeros; superseded, legacy, uncertain-identity and potentially "
                     "duplicated observations are listed but not scored. Not derived from fatigue, distance, preset ability or coach-context evidence "
                     "(leadership, chemistry, pressure situations). Manual entries are human annotations, not automatic recognition."}
