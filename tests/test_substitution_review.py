"""The EXPERIMENTAL substitution review's rules: hysteresis, cooldown, constraints, context policies and missing inputs.
The physiology and performance series are injected directly so each rule is checked on its own. All values are fictional."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.core import substitution as sub
from app.models import Athlete
from app.models.context import ContextEntry
from app.models.roster import RosterEntry
from app.models.substitution import SubstitutionProfile

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 11, 1, 10, 0, 0, tzinfo=UTC)
PROFILE = SubstitutionProfile.model_validate_json((ROOT / "scripts" / "examples" / "review" / "fictional_substitution_profile.json").read_text())


def ts(s):
    return T0 + timedelta(seconds=s)


def roster_entry(aid, status, role=None, able=(), **kw):
    return RosterEntry(id=f"ros-{aid}-{status}", recording_id="r", created_at=ts(0), effective_from=ts(0), source="head_coach", athlete_id=aid, status=status,
                       current_role=role, able_roles=list(able), **kw)


def team(**kw):
    return RosterEntry(id="ros-team", recording_id="r", created_at=ts(0), effective_from=ts(0), source="head_coach", **kw)


def inputs(roster, context=(), at=1200, profile=PROFILE):
    athletes = {a: sub.AthleteData(Athlete(id=a, name=a, jersey_number=n + 1, sport="soccer", team_id="t")) for n, a in enumerate(("a1", "b1", "b2", "a2"))}
    return sub.ReviewInputs("r", ts(0), ts(3000), "a1", ts(at), athletes, list(context), list(roster), (profile, "sha") if profile else None, None, None)


@pytest.fixture
def series(monkeypatch):
    """Inject the index and composite per review point: values[k] for the k-th point, oldest first (the last is `at`)."""
    state = {"phys": [], "perf": [], "identity": 0}

    def phys(inp, aid, t):
        if aid != "a1":
            return {"available": False, "value": None, "reasons": ["no strap"]}
        k = int((t - ts(0)).total_seconds() // PROFILE.step_seconds)
        v = state["phys"][k] if k < len(state["phys"]) else state["phys"][-1]
        return {"available": v is not None, "value": v, "reasons": [] if v is not None else ["stale"]}

    def perf(inp, prof, aid, t):
        k = int((t - ts(0)).total_seconds() // PROFILE.step_seconds)
        v = state["perf"][k] if k < len(state["perf"]) else state["perf"][-1]
        return {"available": v is not None, "value": v, "reasons": [] if v is not None else ["no evidence"], "identity_uncertain_excluded": state["identity"]}
    monkeypatch.setattr(sub, "physiology_at", phys)
    monkeypatch.setattr(sub, "performance_at", perf)
    return state


BASE = [roster_entry("a1", "on_field", "centre back", ["centre back"]), roster_entry("b1", "bench", able=["centre back"]), roster_entry("b2", "bench", able=["forward"])]


def test_track_needs_persistence_and_hysteresis_to_change_state():
    hi, lo = (lambda v: v >= 70), (lambda v: v < 55)
    pts = [(None, v) for v in (40, 90, 40, 80, 80, 80, 60, 50, 60, 50, 50, None, 50)]
    assert sub.track(pts, hi, lo, 3, 2, ("elevated_unconfirmed", "")) == [
        "normal", "elevated_unconfirmed", "normal", "elevated_unconfirmed", "elevated_unconfirmed", "concern",
        "concern", "concern", "concern", "concern", "normal", "unavailable", "normal"], "one high reading never makes a concern; 60 sits in the hysteresis band"


def test_no_profile_and_off_field_are_insufficient_evidence(series):
    series["phys"], series["perf"] = [40], [70]
    r = sub.substitution_review(inputs(BASE, profile=None))
    assert r["outcome"] == "insufficient_evidence" and "no experimental substitution profile" in r["reasons"][0] and r["history"] == []
    r = sub.substitution_review(inputs([roster_entry("a1", "bench", able=["centre back"])]))
    assert r["outcome"] == "insufficient_evidence" and "does not show the athlete on the field" in r["reasons"][0]
    r = sub.substitution_review(inputs([]))
    assert r["outcome"] == "insufficient_evidence" and "no roster entry" in r["reasons"][0], "unrecorded roster state is unknown, not 'on the field'"


def test_physiology_alone_never_recommends_a_replacement(series):
    series["phys"], series["perf"] = [90] * 30, [70] * 30
    r = sub.substitution_review(inputs(BASE))
    assert r["outcome"] == "review_later" and r["triggers"] == ["physiology_elevated"] and r["replacements"] == []
    assert r["review_again_at"] == ts(1200 + PROFILE.review_again_after_seconds).isoformat().replace("+00:00", "Z")


def test_decline_with_physiology_unavailable_waits_and_missing_bench_physiology_is_not_freshness(series):
    series["phys"], series["perf"] = [None] * 30, [30] * 30
    r = sub.substitution_review(inputs(BASE))
    assert r["outcome"] == "review_later" and "requires an available physiological index" in r["reasons"][-1]
    series["phys"] = [40] * 30
    r = sub.substitution_review(inputs(BASE))
    assert r["outcome"] == "consider_replacement" and [(c["athlete_id"], c["role_fit"]) for c in r["replacements"]] == [("b1", "fits"), ("b2", "does_not_fit")]
    assert r["replacements"][0]["physiology"]["available"] is False and "not evidence that this athlete is fresh" in r["replacements"][0]["physiology"]["note"]
    assert "substitutions remaining: unknown" in r["missing"]


@pytest.mark.parametrize("team_kw, outcome, text", [
    ({"keep_on_field": [{"athlete_id": "a1", "reason": "fictional: captain"}]}, "keep", "keep on the field"),
    ({"substitutions_remaining": 0}, "keep", "no substitutions remaining"),
    ({"do_not_bring_on": [{"athlete_id": "b1", "reason": "fictional: rested"}]}, "review_later", "do not bring on b1"),
    ({"no_substitution_before": "2026-11-01T10:30:00Z"}, "review_later", "no substitution before"),
])
def test_coach_constraints_are_applied_and_explained(series, team_kw, outcome, text):
    series["phys"], series["perf"] = [40] * 30, [30] * 30
    r = sub.substitution_review(inputs([*BASE, team(**team_kw)]))
    assert r["outcome"] == outcome and any(text in c for c in r["constraints_applied"]), r
    if "no_substitution_before" in team_kw:
        assert r["review_again_at"] == "2026-11-01T10:30:00Z"


def test_context_evidence_holds_or_is_listed_but_never_scores(series):
    series["phys"], series["perf"] = [40] * 30, [30] * 30
    ctx = ContextEntry(id="c", recording_id="r", created_at=ts(0), effective_from=ts(0), source="head_coach", certainty="confirmed", athlete_id="a1",
                       evidence={"status": "recorded", "items": [{"kind": "pressure_situation", "task": "defending late leads", "description": "fictional", "observed_at": ts(600).isoformat()}]})
    r = sub.substitution_review(inputs(BASE, [ctx]))
    assert r["outcome"] == "review_later" and "no bonus or score is applied" in r["reasons"][-1] and "pressure situation" in r["tradeoffs"][0]
    listed = PROFILE.model_copy(update={"context": PROFILE.context.model_copy(update={"pressure_situation": "list_only"})})
    r = sub.substitution_review(inputs(BASE, [ctx], profile=listed))
    assert r["outcome"] == "consider_replacement" and "pressure situation" in r["tradeoffs"][0]
    future = ctx.model_copy(update={"evidence": ctx.evidence.model_copy(update={"items": [ctx.evidence.items[0].model_copy(update={"observed_at": ts(2000)})]})})
    assert sub.substitution_review(inputs(BASE, [future]))["outcome"] == "consider_replacement", "evidence observed after the review time is not used"


def test_role_mismatch_uses_exact_coach_wording_and_unknown_roles_stay_unknown(series):
    series["phys"], series["perf"] = [40] * 30, [70] * 30
    ctx = ContextEntry(id="c", recording_id="r", created_at=ts(0), effective_from=ts(0), source="head_coach", certainty="confirmed", athlete_id="a1", role={"status": "recorded", "value": "Forward"})
    r = sub.substitution_review(inputs(BASE, [ctx]))
    assert r["outcome"] == "consider_replacement" and r["triggers"] == ["role_mismatch"] and r["replacements"][1]["role_fit"] == "fits", "case-insensitive exact text"
    unknown = [roster_entry("a1", "on_field", "centre back", []), *BASE[1:]]
    r = sub.substitution_review(inputs(unknown, [ctx]))
    assert r["outcome"] == "keep" and any("roles the athlete can play are not recorded" in m for m in r["missing"])


def test_identity_uncertainty_and_missing_everything_are_insufficient(series):
    series["phys"], series["perf"], series["identity"] = [40] * 30, [None] * 30, 6
    r = sub.substitution_review(inputs(BASE))
    assert r["outcome"] == "insufficient_evidence" and "uncertain identity" in r["reasons"][0]
    series["phys"], series["identity"] = [None] * 30, 0
    assert sub.substitution_review(inputs(BASE))["outcome"] == "insufficient_evidence"
    series["phys"] = [40] * 30
    r = sub.substitution_review(inputs(BASE))
    assert r["outcome"] == "keep" and any(m.startswith("performance composite unavailable") for m in r["missing"]), "keep, with the missing evidence listed"


def test_cooldown_holds_back_an_opposite_output(series):
    # a concern builds at point 3 (consider), clears at point 7; the opposite output (keep) is held until the cooldown ends
    series["phys"] = [40] * 30
    series["perf"] = [70] * 3 + [30, 30, 30] + [60, 60] + [70] * 22
    r = sub.substitution_review(inputs(BASE, at=1200))
    hist = r["history"]
    raw, out = [h["raw_outcome"] for h in hist], [h["outcome"] for h in hist]
    i = raw.index("consider_replacement")
    j = raw.index("keep", i)
    assert out[j] == "consider_replacement" and (j - i) * PROFILE.step_seconds < PROFILE.cooldown_seconds, "held back"
    k = next(n for n in range(j, len(out)) if out[n] == "keep")
    assert (k - i) * PROFILE.step_seconds >= PROFILE.cooldown_seconds and r["outcome"] == "keep"
    short = sub.substitution_review(inputs(BASE, at=(i + 3) * 60 + 0))
    assert short["outcome"] == "consider_replacement" and short["raw_outcome"] == "keep" and short["cooldown"]["held"] is True

