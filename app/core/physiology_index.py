"""Baseline statistics and the EXPERIMENTAL physiological index (pure functions; the API layer does the I/O).

BASELINE STATISTICS (calculated once when a baseline is created). Per requested signal, over the recording's samples whose
recorded timestamp lies in any of the ranges (inclusive): a sample without a value is excluded as "missing_value"; with a
minimum signal quality, a sample without a reported quality is excluded as "quality_not_reported" and one below it as
"low_quality"; with a required source, other sources are excluded as "other_source". Mean, sample SD (n - 1), median,
min and max describe the included values. A signal is insufficient with fewer than min_samples values or with zero
variance (every value equal): a z-score against it would divide by zero, so it is never used that way.

INDEX AT TIME T (profile from PHYSIOLOGICAL_INDEX_PROFILE_PATH; none ships, a FICTIONAL example exists).
  1. Baseline: the athlete's newest baseline that the profile's requirements allow (review status, condition, signal
     sample counts). One whose data ends at or before T is preferred; otherwise, only if the profile allows it, the newest
     one using later data, and the result is labelled "retrospective" instead of live-time.
  2. Inputs per signal: samples with T - window < timestamp <= T that were not received after T (receipt metadata, when
     recorded), filtered by the baseline's own quality and source requirements (so both sides are comparable), and, with
     same_device_required, only from devices that contributed to the baseline ("device_changed" otherwise). Later samples
     are never used.
  3. A signal is unavailable when: the baseline lacks it or it is insufficient there (an SDNN baseline is never used for
     RMSSD, or the reverse); its baseline SD is zero (z_score) or mean is zero (percent_change); fewer than
     min_samples_in_window values; coverage (values x expected interval / window, capped at 1) below min_input_coverage;
     or its newest value is older than staleness_seconds.
  4. transformed = z_score or percent_change of the window mean against the baseline; directed = transformed, negated for
     lower_is_more_strain; score = 100 x clamp((directed - zero_at) / (hundred_at - zero_at), 0, 1).
  5. index = weighted mean of the available signals' scores, available only when their weights reach min_weight_coverage
     of the total. Each signal's contribution is weight x score / available weight.
The index describes how far recent signals sit from this athlete's own baseline under a made-up or team-chosen
weighting. It is not a fatigue measurement and does not confirm fatigue, injury, safety or readiness.
"""

from __future__ import annotations

import hashlib
import json
import statistics
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from app.models.baseline import SIGNAL_DEFINITIONS, Baseline, BaselineCreate, IndexProfile, SignalStats

LABEL = "Experimental physiological index: distance from this athlete's own baseline, 0-100. Not a fatigue, injury, safety or readiness measurement."
OTHER_HRV = {"hrv_rmssd_ms": "hrv_sdnn_ms", "hrv_sdnn_ms": "hrv_rmssd_ms"}


class ProfileError(Exception):
    pass


def iso(ts: datetime) -> str:
    return ts.isoformat().replace("+00:00", "Z")


def parse_profile(data: bytes, where: str = "the physiological index profile") -> tuple[IndexProfile, str]:
    try:
        return IndexProfile.model_validate_json(data), hashlib.sha256(data).hexdigest()
    except Exception as exc:  # noqa: BLE001
        raise ProfileError(f"{where} is invalid: {exc}") from exc


def load_profile(path: str | Path | None) -> tuple[IndexProfile | None, str | None, bytes | None]:
    if not path:
        return None, None, None
    data = Path(path).read_bytes()
    profile, sha = parse_profile(data, f"the physiological index profile {path}")
    return profile, sha, data


def _included(sample, signal: str, min_quality: float | None, source) -> tuple[float | None, str | None]:
    value = getattr(sample, signal)
    if value is None:
        return None, "missing_value"
    if min_quality is not None:
        if sample.signal_quality is None:
            return None, "quality_not_reported"
        if sample.signal_quality < min_quality:
            return None, "low_quality"
    if source is not None and sample.source != source:
        return None, "other_source"
    return value, None


def baseline_stats(body: BaselineCreate, samples: list) -> dict[str, SignalStats]:
    """Per-signal statistics over the samples whose timestamps lie in any range (see the module docstring)."""
    chosen = sorted((s for s in samples if any(r.start <= s.timestamp <= r.end for r in body.ranges)), key=lambda s: s.timestamp)
    out = {}
    for signal in body.signals:
        vals, stamps, excluded, devices, sources = [], [], Counter(), set(), Counter()
        for s in chosen:
            v, why = _included(s, signal, body.min_signal_quality, body.required_source)
            if why:
                excluded[why] += 1
                continue
            vals.append(v); stamps.append(s.timestamp); devices.add(s.device_id); sources[str(s.source)] += 1
        sd = statistics.stdev(vals) if len(vals) >= 2 else None
        reason = None
        if len(vals) < body.min_samples:
            reason = f"{len(vals)} included values, fewer than the minimum of {body.min_samples}"
        elif sd == 0:
            reason = "zero variance: every included value is equal, so a z-score against this baseline would divide by zero"
        out[signal] = SignalStats(signal=signal, definition=SIGNAL_DEFINITIONS[signal], included=len(vals), excluded=dict(excluded),
                                  mean=statistics.fmean(vals) if vals else None, sd=sd, median=statistics.median(vals) if vals else None,
                                  min=min(vals) if vals else None, max=max(vals) if vals else None, first_at=stamps[0] if stamps else None,
                                  last_at=stamps[-1] if stamps else None, device_ids=sorted(devices), sources=dict(sources),
                                  sufficient=reason is None, insufficient_reason=reason)
    return out


def select_baseline(baselines: list[Baseline], profile: IndexProfile, at: datetime) -> tuple[Baseline | None, str | None, list[str]]:
    """(baseline, "live_time" | "retrospective", reasons it is unavailable)."""
    req = profile.baseline
    ok_status = ("accepted",) if req.require_status == "accepted" else ("accepted", "pending_review")
    reasons = []
    usable = []
    for b in baselines:
        if b.status not in ok_status:
            reasons.append(f"baseline v{b.version} ({b.id}) is {b.status}; the profile requires {req.require_status}")
            continue
        if req.conditions is not None and b.condition not in req.conditions:
            reasons.append(f"baseline v{b.version} ({b.id}) was taken during {b.condition}; the profile allows {', '.join(req.conditions)}")
            continue
        usable.append(b)
    if not baselines:
        reasons.append("the athlete has no physiological baseline")
    live = [b for b in usable if b.data_end <= at]
    if live:
        return max(live, key=lambda b: (b.created_at, b.version)), "live_time", []
    later = [b for b in usable if b.data_end > at]
    if later and req.allow_retrospective:
        return max(later, key=lambda b: (b.created_at, b.version)), "retrospective", []
    if later:
        reasons.append("the only usable baselines use data recorded after the review time, and the profile does not allow a retrospective baseline")
    return None, None, reasons


def compute_index(profile: IndexProfile, profile_sha: str, baseline: Baseline | None, timing: str | None, samples: list[tuple], at: datetime,
                  baseline_reasons: list[str] | None = None) -> dict:
    """The index at `at` from (sample, receipt-or-None) pairs; only samples within the window and received by `at` are used."""
    start = at - timedelta(seconds=profile.window_seconds)
    window, received_late = [], 0
    for s, receipt in samples:
        if not (start < s.timestamp <= at):
            continue
        if receipt and datetime.fromisoformat(receipt["received_at"].replace("Z", "+00:00")) > at:
            received_late += 1
            continue
        window.append(s)
    head = {"at": iso(at), "label": LABEL, "fictional_profile": profile.fictional,
            "profile": {"version": profile.version, "sha256": profile_sha, "fictional": profile.fictional},
            "window": {"start_exclusive": iso(start), "end_inclusive": iso(at), "seconds": profile.window_seconds},
            "samples_in_window": len(window), "excluded_received_after_review_time": received_late,
            "window_sources": dict(Counter(str(s.source) for s in window))}
    if baseline is None:
        return {**head, "available": False, "value": None, "timing": None, "baseline": None, "signals": [], "input_coverage": 0.0,
                "reasons": baseline_reasons or ["no usable baseline"]}
    total_w = sum(s.weight for s in profile.signals)
    rows, avail_w = [], 0.0
    for spec in profile.signals:
        row = {"signal": spec.signal, "weight": spec.weight, "transform": spec.transform, "direction": spec.direction, "available": False}
        st = baseline.stats.get(spec.signal)
        if st is None:
            other = OTHER_HRV.get(spec.signal)
            row["reason"] = f"the baseline has no {spec.signal}" + (f"; its {other} values are a different statistic and are not substituted" if other and other in baseline.stats else "")
            rows.append(row); continue
        row.update(baseline_mean=st.mean, baseline_sd=st.sd, baseline_n=st.included)
        if not st.sufficient:
            row["reason"] = f"baseline signal insufficient: {st.insufficient_reason}"
            rows.append(row); continue
        if st.included < profile.baseline.min_samples:
            row["reason"] = f"the baseline has {st.included} values, fewer than the profile's minimum of {profile.baseline.min_samples}"
            rows.append(row); continue
        if spec.transform == "z_score" and not st.sd:
            row["reason"] = "baseline SD is zero; a z-score is undefined"
            rows.append(row); continue
        if spec.transform == "percent_change" and not st.mean:
            row["reason"] = "baseline mean is zero; a percent change is undefined"
            rows.append(row); continue
        vals, stamps, excluded = [], [], Counter()
        for s in window:
            v, why = _included(s, spec.signal, baseline.min_signal_quality, baseline.required_source)
            if why is None and profile.baseline.same_device_required and s.device_id not in st.device_ids:
                why = "device_changed"
            if why:
                excluded[why] += 1
                continue
            vals.append(v); stamps.append(s.timestamp)
        coverage = min(1.0, len(vals) * profile.expected_interval_seconds / profile.window_seconds)
        row.update(window_n=len(vals), window_excluded=dict(excluded), coverage=round(coverage, 4))
        if not vals:
            row["reason"] = "no usable values in the window" + (" (every value came from a device that did not contribute to the baseline)" if excluded.get("device_changed") and not excluded.keys() - {"device_changed"} else "")
            rows.append(row); continue
        age = (at - max(stamps)).total_seconds()
        row["newest_age_s"] = round(age, 3)
        if len(vals) < spec.min_samples_in_window:
            row["reason"] = f"{len(vals)} values in the window, fewer than {spec.min_samples_in_window}"
        elif coverage < profile.min_input_coverage:
            row["reason"] = f"input coverage {coverage:.2f} below {profile.min_input_coverage}"
        elif age > profile.staleness_seconds:
            row["reason"] = f"newest value is {age:.0f} s old (stale after {profile.staleness_seconds:g} s)"
        else:
            m = statistics.fmean(vals)
            t = (m - st.mean) / st.sd if spec.transform == "z_score" else 100 * (m - st.mean) / st.mean
            d = t if spec.direction == "higher_is_more_strain" else -t
            score = 100 * min(1.0, max(0.0, (d - spec.zero_at) / (spec.hundred_at - spec.zero_at)))
            row.update(available=True, window_mean=round(m, 4), transformed=round(t, 4), directed=round(d, 4), score=round(score, 2), _score=score)
            avail_w += spec.weight
        rows.append(row)
    value, reasons = None, []
    share = avail_w / total_w
    if share >= profile.min_weight_coverage and avail_w > 0:
        value = round(sum(r["_score"] * r["weight"] for r in rows if r["available"]) / avail_w, 2)   # unrounded scores; rounding is for display
        for r in rows:
            if r["available"]:
                r["contribution"] = round(r["_score"] * r["weight"] / avail_w, 2)
    for r in rows:
        r.pop("_score", None)
    if value is None:
        reasons.append(f"available signals carry {share:.0%} of the weight; the profile needs {profile.min_weight_coverage:.0%}")
        reasons += [f"{r['signal']}: {r['reason']}" for r in rows if not r["available"]]
    if timing == "retrospective":
        reasons.append("retrospective: the baseline uses data recorded after the review time, so this is not what could have been calculated live")
    return {**head, "available": value is not None, "value": value, "timing": timing,
            "baseline": {"id": baseline.id, "version": baseline.version, "status": baseline.status, "condition": baseline.condition, "data_start": iso(baseline.data_start),
                         "data_end": iso(baseline.data_end), "created_after_review_time": baseline.created_at > at, "source_recording_id": baseline.source_recording_id},
            "signals": rows, "input_coverage": round(share, 4), "reasons": reasons}


def history(profile: IndexProfile, profile_sha: str, baselines: list[Baseline], samples: list[tuple], start: datetime, end: datetime, step_s: float) -> list[dict]:
    """The index at start + window, + step, ... up to end (each point uses only data available at that point)."""
    out = []
    t = start + timedelta(seconds=profile.window_seconds)
    while t <= end + timedelta(microseconds=1):
        b, timing, why = select_baseline(baselines, profile, t)
        out.append(compute_index(profile, profile_sha, b, timing, samples, t, why))
        t += timedelta(seconds=step_s)
    return out


def samples_digest(samples: list[tuple]) -> str:
    """SHA-256 over the samples and their receipt times, to detect later changes when reproducing a saved review (the
    receipt's origin is left out: an imported copy of the same samples gives the same digest)."""
    h = hashlib.sha256()
    for s, receipt in samples:
        h.update(s.model_dump_json().encode()); h.update(json.dumps((receipt or {}).get("received_at")).encode()); h.update(b"\n")
    return h.hexdigest()
