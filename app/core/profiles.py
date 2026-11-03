"""Readable inspection of experimental profiles (index profile, substitution profile, performance rubric) with the same
parsers the engine uses: version, SHA-256 of the exact bytes, fictional status, a summary of what each profile sets,
configuration errors, and compatibility problems between the profiles configured together.

Inspection never edits a profile. A changed profile is a different file with a different hash (and should carry a
new `version`); saved reviews keep the exact text they were calculated with and reproduce from it.
"""

from __future__ import annotations

import hashlib
import json

from app.core.performance import RubricError, parse_rubric
from app.core.physiology_index import ProfileError, parse_profile
from app.models.substitution import SubstitutionProfile

KINDS = {"athletrix-physiological-index-profile": "physiological_index", "athletrix-substitution-profile": "substitution",
         "athletrix-performance-rubric": "performance_rubric"}


def inspect(data: bytes, name: str = "profile") -> dict:
    """One profile file: kind, version, hash, fictional flag, summary and errors (never raises for a bad file)."""
    sha = hashlib.sha256(data).hexdigest()
    out = {"name": name, "sha256": sha, "kind": None, "version": None, "fictional": None, "valid": False, "errors": [], "warnings": [], "summary": {}}
    try:
        doc = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        out["errors"].append(f"not valid JSON: {exc}")
        return out
    kind = KINDS.get(doc.get("format")) if isinstance(doc, dict) else None
    if kind is None:
        out["errors"].append(f"unknown format {doc.get('format') if isinstance(doc, dict) else None!r}; expected one of {', '.join(KINDS)}")
        return out
    out.update(kind=kind, version=doc.get("version"), fictional=doc.get("fictional"))
    try:
        if kind == "physiological_index":
            p, _ = parse_profile(data, name)
            out["summary"] = {"window_s": p.window_seconds, "expected_interval_s": p.expected_interval_seconds, "min_input_coverage": p.min_input_coverage,
                              "staleness_s": p.staleness_seconds, "min_weight_coverage": p.min_weight_coverage, "baseline": p.baseline.model_dump(mode="json"),
                              "signals": [s.model_dump(mode="json") for s in p.signals]}
            if p.staleness_seconds > p.window_seconds:
                out["warnings"].append("staleness_seconds exceeds the window: a value can be stale only if it is also outside the window, so the staleness rule never applies")
            total = sum(s.weight for s in p.signals)
            if max(s.weight for s in p.signals) / total >= p.min_weight_coverage:
                out["warnings"].append("one signal alone carries enough weight for an index: the index can rest on a single signal")
            if p.baseline.allow_retrospective:
                out["warnings"].append("retrospective baselines are allowed: results before the baseline's data end are labelled retrospective")
        elif kind == "performance_rubric":
            r, _ = parse_rubric(data, name)
            out["summary"] = {"areas": {a: {"weight": spec.weight, "measures": [m.model_dump(mode="json") for m in spec.measures]} for a, spec in r.areas.items()}}
            missing = [a for a in ("execution", "decision_making", "movement_positioning", "coach_strategy") if a not in r.areas]
            if missing:
                out["warnings"].append(f"no settings for {', '.join(missing)}: the composite is never available")
        else:
            p = SubstitutionProfile.model_validate_json(data)
            out["summary"] = {k: v for k, v in p.model_dump(mode="json").items() if k not in ("format", "schema_version", "version", "fictional", "note")}
            for what, rule in (("physiology", p.physiology), ("performance", p.performance)):
                if rule.persistence_points * p.step_seconds > p.lookback_seconds:
                    out["errors"].append(f"{what}: {rule.persistence_points} points x {p.step_seconds:g} s exceeds the {p.lookback_seconds:g} s lookback, so a sustained state can never form")
            if p.cooldown_seconds > p.lookback_seconds:
                out["warnings"].append("the cooldown is longer than the lookback: a hold can outlast the history used to explain it")
    except (ProfileError, RubricError, ValueError) as exc:
        out["errors"].append(str(exc))
        return out
    if out["fictional"]:
        out["warnings"].append("FICTIONAL: made-up values for tests and demonstrations; not the team's settings")
    out["valid"] = not out["errors"]
    return out


def compatibility(found: dict[str, dict | None]) -> list[str]:
    """Problems between the profiles configured together (kind -> inspection or None)."""
    problems = []
    sub, idx, rub = found.get("substitution"), found.get("physiological_index"), found.get("performance_rubric")
    if sub and sub["valid"]:
        if idx is None:
            problems.append("a substitution profile is configured without a physiological index profile: physiology is always unavailable")
        if rub is None:
            problems.append("a substitution profile is configured without a performance rubric: performance is always unavailable")
        if idx and idx["valid"] and idx["summary"]["window_s"] > sub["summary"]["step_seconds"] * sub["summary"]["physiology"]["persistence_points"]:
            problems.append("the index window is longer than the physiology persistence span: consecutive points share most of their data, so persistence adds little")
    fictional = {k for k, v in found.items() if v and v.get("fictional")}
    if fictional and len(fictional) != len([v for v in found.values() if v]):
        problems.append(f"fictional and non-fictional profiles are mixed ({', '.join(sorted(fictional))} fictional): results stay labelled fictional")
    return problems
