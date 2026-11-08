"""EXPERIMENTAL fatigue-pilot scores: three candidates (vest-only, camera-only, combined) relative to the athlete's own
baseline, each explained feature by feature (pure functions; the API layer reads the data).

PROFILE (format "athletrix-fatigue-profile", schema 1; FATIGUE_PROFILE_PATH; none ships, a FICTIONAL development
example is scripts/examples/pilot/fictional_fatigue_profile.json). Each feature declares its true inputs; a candidate
may only contain features whose inputs are among its streams (heart rate relative to camera speed is combined, so it can
never enter the vest-only candidate). Validation refuses a profile that breaks this.

WHAT A SCORE MAY SEE at cutoff T
  - samples with timestamp <= T and, as entered, received by T (samples without receipt metadata are assumed available
    at their own timestamp, and this is labelled);
  - physiology only from the athlete's OWN wearer assignments (from the assignment's start until the next one) and only
    from the assigned device; the baseline and maximum heart rate are the ones in that assignment;
  - never samples of either stream inside a test-effort exclusion: every marked timed sprint from pre_s before its start
    to post_s after its end (or after assumed_duration_if_no_end_s when no end is marked). Rolling and reference windows
    simply have fewer samples there; nothing bridges across.
  - never timed sprint results, self-ratings or coach labels (they are not inputs of this module at all).

FEATURES (each 0-100 or unavailable with a reason; a real 0 is a value)
  hr_strain            wearable: mean HR over window_s as a fraction of heart-rate reserve
                       (mean - baseline rest HR) / (HR max - rest); 0 at zero_at_hrr, 100 at hundred_at_hrr.
  time_above_threshold wearable: elapsed time with HR >= threshold_fraction_of_hr_max x HR max over window_s, as a
                       share of covered time (intervals between samples longer than max_gap_s are not counted);
                       0 at none, 100 at hundred_at_fraction. Needs min_coverage of the window covered.
  recovery             wearable: the latest qualifying effort (HR >= qualifying fraction x HR max) followed by a complete
                       recovery_s window with no new qualifying effort and min_coverage covered; the HR drop from the
                       effort's end to the mean of the window's last end_average_s. 0 when the drop reaches
                       reference_drop_bpm, 100 at no drop. No usable window = unavailable (never 0).
  hr_per_speed         wearable + camera: mean HR / mean camera speed over pairs (HR sample and the nearest tracking
                       sample within pair_tolerance_s, speed >= min_speed_mps: near-zero speed is excluded) in the recent
                       window, against the same ratio in the athlete's own camera reference window; 100 at
                       hundred_at_increase. Needs a stated camera clock alignment (capture mapping or operator estimate).
  output_change        camera: mean speed over covered time (distance per second) in the recent window against the
                       reference window; 0 when not lower, 100 at hundred_at_drop.
  sprint_quality       camera: mean peak speed of detected sprints (app/core/sprints.py) in the recent window against
                       those of the reference window. No sprint in the recent window = no observed opportunity:
                       unavailable, never a measured drop.
CANDIDATES: weighted mean of available feature scores (weights renormalised over available features) when every
`required` feature is available, the candidate's streams are fresh (newest sample at most staleness_s old) and the
available weights reach min_weight_coverage. Otherwise unavailable with every reason. The combined candidate is never
turned into a one-source score.
"""

from __future__ import annotations

import hashlib
import json
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.core.sprints import SprintRule, detect

FORMAT = "athletrix-fatigue-profile"
CANDIDATES = ("vest_only", "camera_only", "combined")
FEATURES = ("hr_strain", "time_above_threshold", "recovery", "hr_per_speed", "output_change", "sprint_quality")
FEATURE_INPUTS = {"hr_strain": {"wearable"}, "time_above_threshold": {"wearable"}, "recovery": {"wearable"}, "hr_per_speed": {"wearable", "camera"},
                  "output_change": {"camera"}, "sprint_quality": {"camera"}}
DISPLAY = ("0-100: 0 = at the athlete's own reference (a real zero), 100 = at or beyond the profile's bound, null = unavailable "
           "(never 0). Experimental estimate relative to the athlete's own baseline; not a fatigue measurement.")
LABEL = "Experimental fatigue estimate (pilot): relative to this athlete's own baseline; the coach decides; no substitution advice."


class FatigueProfileError(ValueError):
    pass


def iso(t: datetime | None) -> str | None:
    return None if t is None else t.isoformat().replace("+00:00", "Z")


def parse_profile(text: str) -> tuple[dict, str]:
    try:
        p = json.loads(text)
    except ValueError as exc:
        raise FatigueProfileError(f"the fatigue profile is not JSON ({exc})") from exc
    e = []
    if not isinstance(p, dict) or p.get("format") != FORMAT or p.get("schema_version") != 1:
        raise FatigueProfileError(f"not an {FORMAT} schema 1 document")
    for k in ("version", "fictional", "features", "candidates", "streams", "test_effort_exclusion", "reference", "baseline"):
        if k not in p:
            e.append(f"missing {k}")
    if e:
        raise FatigueProfileError("; ".join(e))
    for f, spec in p["features"].items():
        if f not in FEATURES:
            e.append(f"unknown feature {f}")
        elif set(spec.get("inputs", [])) != FEATURE_INPUTS[f]:
            e.append(f"feature {f} must declare its true inputs {sorted(FEATURE_INPUTS[f])}")
    for c in CANDIDATES:
        spec = p["candidates"].get(c)
        if spec is None:
            e.append(f"missing candidate {c}")
            continue
        streams = set(spec.get("streams", []))
        for f, w in spec.get("weights", {}).items():
            if f not in p["features"]:
                e.append(f"{c}: feature {f} is not defined")
            elif not FEATURE_INPUTS[f] <= streams:
                e.append(f"{c}: feature {f} uses {sorted(FEATURE_INPUTS[f])}, not only {sorted(streams)}")
            if not (isinstance(w, (int, float)) and w > 0):
                e.append(f"{c}: weight of {f} must be positive")
        for f in spec.get("required", []):
            if f not in spec.get("weights", {}):
                e.append(f"{c}: required feature {f} has no weight")
    if p["candidates"].get("vest_only", {}).get("streams") != ["wearable"] or p["candidates"].get("camera_only", {}).get("streams") != ["camera"]:
        e.append("vest_only must use only the wearable stream and camera_only only the camera stream")
    if e:
        raise FatigueProfileError("; ".join(e))
    return p, hashlib.sha256(text.encode()).hexdigest()


@dataclass
class Wearer:
    start: datetime
    end: datetime | None
    device_id: str
    participant_code: str
    baseline: dict | None          # {"id", "rest_hr_bpm", "n", "status"} or None
    hr_max_bpm: float | None
    hr_max_origin: str | None


@dataclass
class ScoreInputs:
    athlete_id: str
    cutoff: datetime
    knowledge: str
    phys: list[tuple] = field(default_factory=list)       # (PhysiologicalSample, receipt dict or None)
    track: list[tuple] = field(default_factory=list)      # (TrackingSample, receipt dict or None)
    wearers: list[Wearer] = field(default_factory=list)   # this athlete's assignments
    exclusions: list[tuple] = field(default_factory=list)  # (start, end, reason)
    reference_start: datetime | None = None               # the athlete's first round_start of the session
    camera_alignment: dict | None = None                  # the session's camera clock reference, or None


def _received(r) -> datetime | None:
    return datetime.fromisoformat(r["received_at"].replace("Z", "+00:00")) if r and r.get("received_at") else None


def _clip(x: float) -> float:
    return max(0.0, min(100.0, x))


def select(inp: ScoreInputs, prof: dict) -> dict:
    """The samples a score may use at the cutoff, with counts of everything left out and why."""
    excl = {"after_cutoff": 0, "received_after_cutoff": 0, "test_effort": 0, "outside_own_wearer_assignment": 0, "other_device": 0,
            "low_confidence_or_predicted": 0}
    no_receipt = 0

    def visible(s, r):
        if s.timestamp > inp.cutoff:
            excl["after_cutoff"] += 1
            return False
        rec = _received(r)
        if inp.knowledge == "as_entered" and rec is not None and rec > inp.cutoff:
            excl["received_after_cutoff"] += 1
            return False
        if any(a <= s.timestamp <= b for a, b, _ in inp.exclusions):
            excl["test_effort"] += 1
            return False
        return True
    phys = []
    for s, r in inp.phys:
        if not visible(s, r):
            continue
        w = next((w for w in inp.wearers if w.start <= s.timestamp and (w.end is None or s.timestamp < w.end)), None)
        if w is None:
            excl["outside_own_wearer_assignment"] += 1
            continue
        if s.device_id != w.device_id:
            excl["other_device"] += 1
            continue
        no_receipt += r is None
        phys.append(s)
    track, invalid, all_track = [], [], []
    minc = prof["streams"].get("min_tracking_confidence", 0.5)
    for s, r in inp.track:
        if not visible(s, r):
            continue
        if s.tracking_confidence < minc or (s.value_basis or "observed") == "predicted":
            excl["low_confidence_or_predicted"] += 1
            invalid.append(s.timestamp)
            all_track.append(s)
            continue
        all_track.append(s)
        no_receipt += r is None
        track.append(s)
    phys.sort(key=lambda s: s.timestamp)
    track.sort(key=lambda s: s.timestamp)
    all_track.sort(key=lambda s: s.timestamp)
    return {"phys": phys, "track": track, "all_track": all_track, "invalid_camera_times": sorted(invalid), "excluded": excl, "without_receipt_metadata": no_receipt}


def _window(samples, t0, t1):
    return [s for s in samples if t0 < s.timestamp <= t1]


def _covered(samples, max_gap, value=lambda s: 1.0, breaks=()):
    """(covered seconds, integral of value over covered time) with left-point values. Never bridged: gaps > max_gap,
    segment changes, and any `breaks` (an excluded interval (start, end) or an invalid sample's time) between two samples."""
    cov = acc = 0.0
    for a, b in zip(samples, samples[1:]):
        dt = (b.timestamp - a.timestamp).total_seconds()
        if dt <= 0 or dt > max_gap or getattr(a, "segment_id", None) != getattr(b, "segment_id", None):
            continue
        if any((x[0] < b.timestamp and x[1] > a.timestamp) if isinstance(x, tuple) else (a.timestamp < x < b.timestamp) for x in breaks):
            continue
        cov += dt
        acc += value(a) * dt
    return cov, acc


def _unavailable(reason: str, **extra) -> dict:
    return {"available": False, "value": None, "reason": reason, **extra}


def features(inp: ScoreInputs, prof: dict, sel: dict) -> dict:
    F, T = prof["features"], inp.cutoff
    gap = prof["streams"]["max_gap_s"]
    excl_iv = [(a, b) for a, b, _ in inp.exclusions]
    cam_breaks = excl_iv + sel["invalid_camera_times"]
    phys = [s for s in sel["phys"] if s.heart_rate_bpm is not None]
    track = sel["track"]
    wearer = next((w for w in inp.wearers if w.start <= T and (w.end is None or T < w.end)), None)
    out = {}
    vest_ok = None
    if wearer is None:
        vest_ok = "the athlete is not the active wearer at the cutoff (no wearer assignment in force)"
    elif not wearer.baseline:
        vest_ok = "no usable baseline in the wearer assignment"
    elif wearer.baseline.get("status") != prof["baseline"]["require_status"]:
        vest_ok = f"the assigned baseline is {wearer.baseline.get('status')}, the profile requires {prof['baseline']['require_status']}"
    rest = wearer.baseline["rest_hr_bpm"] if wearer and wearer.baseline else None
    hrmax = wearer.hr_max_bpm if wearer else None

    if "hr_strain" in F:
        f = F["hr_strain"]
        w = _window(phys, T - timedelta(seconds=f["window_s"]), T)
        base = {"window": {"start": iso(T - timedelta(seconds=f["window_s"])), "end": iso(T)}, "inputs": ["wearable"], "samples": len(w),
                "reference": {"baseline_id": wearer.baseline["id"] if wearer and wearer.baseline else None, "rest_hr_bpm": rest, "hr_max_bpm": hrmax,
                              "hr_max_origin": wearer.hr_max_origin if wearer else None}}
        if vest_ok:
            out["hr_strain"] = {**base, **_unavailable(vest_ok)}
        elif hrmax is None:
            out["hr_strain"] = {**base, **_unavailable("no maximum heart rate (with its origin) in the wearer assignment")}
        elif len(w) < f["min_samples"]:
            out["hr_strain"] = {**base, **_unavailable(f"{len(w)} heart-rate values in the window, fewer than {f['min_samples']}")}
        elif hrmax <= rest:
            out["hr_strain"] = {**base, **_unavailable("the maximum heart rate is not above the resting baseline")}
        else:
            m = statistics.fmean(s.heart_rate_bpm for s in w)
            hrr = (m - rest) / (hrmax - rest)
            out["hr_strain"] = {**base, "available": True, "measure": {"mean_hr_bpm": round(m, 2), "fraction_of_reserve": round(hrr, 4)},
                                "value": round(_clip(100 * (hrr - f["zero_at_hrr"]) / (f["hundred_at_hrr"] - f["zero_at_hrr"])), 2)}

    if "time_above_threshold" in F:
        f = F["time_above_threshold"]
        t0 = T - timedelta(seconds=f["window_s"])
        w = _window(phys, t0, T)
        base = {"window": {"start": iso(t0), "end": iso(T)}, "inputs": ["wearable"], "samples": len(w)}
        if vest_ok or hrmax is None:
            out["time_above_threshold"] = {**base, **_unavailable(vest_ok or "no maximum heart rate (with its origin) in the wearer assignment")}
        else:
            thr = f["threshold_fraction_of_hr_max"] * hrmax
            cov, above = _covered(w, gap, lambda s: 1.0 if s.heart_rate_bpm >= thr else 0.0, excl_iv)
            base["reference"] = {"threshold_bpm": round(thr, 1), "hr_max_bpm": hrmax}
            if cov < f["min_coverage"] * f["window_s"]:
                out["time_above_threshold"] = {**base, **_unavailable(f"{cov:.0f} s of the {f['window_s']} s window covered (gaps over {gap:g} s are not bridged); needs {f['min_coverage']:.0%}")}
            else:
                frac = above / cov
                out["time_above_threshold"] = {**base, "available": True, "measure": {"seconds_above": round(above, 1), "covered_s": round(cov, 1), "fraction": round(frac, 4)},
                                               "value": round(_clip(100 * frac / f["hundred_at_fraction"]), 2)}

    if "recovery" in F:
        f = F["recovery"]
        base = {"inputs": ["wearable"]}
        if vest_ok or hrmax is None:
            out["recovery"] = {**base, **_unavailable(vest_ok or "no maximum heart rate (with its origin) in the wearer assignment")}
        else:
            q = f["qualifying_fraction_of_hr_max"] * hrmax
            look = _window(phys, T - timedelta(seconds=f["lookback_s"]), T)
            qual = [s for s in look if s.heart_rate_bpm >= q]
            chosen, why = None, "no qualifying effort (heart rate at or above the qualifying level) in the lookback"
            for e in reversed(qual):
                end = e.timestamp + timedelta(seconds=f["recovery_s"])
                if end > T:
                    why = "the latest qualifying effort's recovery window is not complete at the cutoff"
                    continue
                rw = [s for s in look if e.timestamp <= s.timestamp <= end]
                if any(s.heart_rate_bpm >= q for s in rw[1:]):
                    why = "a new qualifying effort interrupted the recovery window"
                    continue
                cov, _ = _covered(rw, gap, breaks=excl_iv)
                if cov < f["min_coverage"] * f["recovery_s"]:
                    why = f"the recovery window is covered for {cov:.0f} of {f['recovery_s']} s"
                    break
                chosen = (e, end, rw)
                break
            if chosen is None:
                out["recovery"] = {**base, **_unavailable(why)}
            else:
                e, end, rw = chosen
                tail = [s.heart_rate_bpm for s in rw if s.timestamp >= end - timedelta(seconds=f["end_average_s"])]
                drop = e.heart_rate_bpm - statistics.fmean(tail)
                out["recovery"] = {**base, "available": True, "window": {"start": iso(e.timestamp), "end": iso(end)},
                                   "measure": {"effort_end_hr_bpm": e.heart_rate_bpm, "drop_bpm": round(drop, 2)}, "reference": {"reference_drop_bpm": f["reference_drop_bpm"]},
                                   "value": round(_clip(100 * (1 - drop / f["reference_drop_bpm"])), 2)}

    # camera reference window: the athlete's own first reference_s of tracked play after their first round_start
    R = prof["reference"]["camera_reference_s"]
    ref_ok, r0, r1 = None, None, None
    if inp.reference_start is None:
        ref_ok = "no round has started for this athlete: the camera reference window is not defined"
    else:
        r0, r1 = inp.reference_start, inp.reference_start + timedelta(seconds=R)
        if r1 > T:
            ref_ok = f"the camera reference window ({R:g} s from the first round start) is not complete at the cutoff"

    def recent_after_ref(window_s):
        t0 = T - timedelta(seconds=window_s)
        if r1 is not None and t0 < r1:
            return None, "the recent window overlaps the reference window"
        return t0, None

    if "output_change" in F:
        f = F["output_change"]
        base = {"inputs": ["camera"], "reference_window": {"start": iso(r0), "end": iso(r1)}}
        t0, why = (None, ref_ok) if ref_ok else recent_after_ref(f["window_s"])
        if why:
            out["output_change"] = {**base, **_unavailable(why)}
        else:
            ref = _window(track, r0, r1)
            rec = _window(track, t0, T)
            cr, dr = _covered(ref, gap, lambda s: s.speed_mps, cam_breaks)
            cn, dn = _covered(rec, gap, lambda s: s.speed_mps, cam_breaks)
            base["window"] = {"start": iso(t0), "end": iso(T)}
            if cr < f["min_coverage"] * R or cn < f["min_coverage"] * f["window_s"]:
                out["output_change"] = {**base, **_unavailable(f"tracked coverage too low (reference {cr:.0f} of {R:g} s, recent {cn:.0f} of {f['window_s']} s; needs {f['min_coverage']:.0%})")}
            elif dr <= 0:
                out["output_change"] = {**base, **_unavailable("no movement in the reference window")}
            else:
                ratio = (dn / cn) / (dr / cr)
                out["output_change"] = {**base, "available": True, "measure": {"recent_mean_speed_mps": round(dn / cn, 3), "reference_mean_speed_mps": round(dr / cr, 3),
                                                                               "change": round(ratio - 1, 4)},
                                        "value": round(_clip(100 * (1 - ratio) / f["hundred_at_drop"]), 2)}

    if "sprint_quality" in F:
        f = F["sprint_quality"]
        base = {"inputs": ["camera"], "reference_window": {"start": iso(r0), "end": iso(r1)}}
        t0, why = (None, ref_ok) if ref_ok else recent_after_ref(f["window_s"])
        if why:
            out["sprint_quality"] = {**base, **_unavailable(why)}
        else:
            rule = SprintRule(threshold_mps=f["threshold_mps"], min_samples=f["min_samples"], max_gap_s=min(gap, 0.5),
                              min_confidence=prof["streams"].get("min_tracking_confidence", 0.5))
            # the detector sees invalid samples too, so an uncertain stretch ends a sprint instead of being bridged;
            # a sprint still in progress at the window's end has no observed peak and is not used
            rs = [x for x in detect(_window(sel["all_track"], r0, r1), rule) if x["complete"]]
            ns = [x for x in detect(_window(sel["all_track"], t0, T), rule) if x["complete"]]
            base.update(window={"start": iso(t0), "end": iso(T)}, reference_sprints=len(rs), recent_sprints=len(ns))
            if not rs:
                out["sprint_quality"] = {**base, **_unavailable("no sprint detected in the reference window")}
            elif not ns:
                out["sprint_quality"] = {**base, **_unavailable("no sprint opportunity observed in the recent window (not a measured drop)")}
            else:
                pr, pn = statistics.fmean(x["peak_speed_mps"] for x in rs), statistics.fmean(x["peak_speed_mps"] for x in ns)
                drop = 1 - pn / pr
                out["sprint_quality"] = {**base, "available": True, "measure": {"recent_mean_peak_mps": round(pn, 3), "reference_mean_peak_mps": round(pr, 3), "drop": round(drop, 4)},
                                         "value": round(_clip(100 * drop / f["hundred_at_drop"]), 2)}

    if "hr_per_speed" in F:
        f = F["hr_per_speed"]
        base = {"inputs": ["wearable", "camera"], "reference_window": {"start": iso(r0), "end": iso(r1)}}
        al = inp.camera_alignment
        t0, why = (None, vest_ok or ref_ok) if (vest_ok or ref_ok) else recent_after_ref(f["window_s"])
        if not why and (al is None or al.get("basis") not in ("capture_time_mapping", "operator_estimate")):
            why = ("no stated camera clock alignment: heart rate and camera speed cannot be paired" if al is None else
                   f"the camera clock reference is {al.get('basis')}: not capture time, so heart rate and camera speed cannot be paired")
        if not why and al.get("uncertainty_s") is not None and al["uncertainty_s"] > f["pair_tolerance_s"]:
            why = f"camera clock uncertainty {al['uncertainty_s']} s exceeds the pairing tolerance {f['pair_tolerance_s']} s"
        if why:
            out["hr_per_speed"] = {**base, **_unavailable(why)}
        else:
            def pairs(a, b):
                hs, ts = _window(phys, a, b), _window(track, a, b)
                res, j, slow = [], 0, 0
                for h in hs:
                    while j + 1 < len(ts) and abs((ts[j + 1].timestamp - h.timestamp).total_seconds()) <= abs((ts[j].timestamp - h.timestamp).total_seconds()):
                        j += 1
                    if ts and abs((ts[j].timestamp - h.timestamp).total_seconds()) <= f["pair_tolerance_s"]:
                        if ts[j].speed_mps < f["min_speed_mps"]:
                            slow += 1
                        else:
                            res.append((h.heart_rate_bpm, ts[j].speed_mps))
                return res, slow
            pr, sr = pairs(r0, r1)
            pn, sn = pairs(t0, T)
            base.update(window={"start": iso(t0), "end": iso(T)}, pairs={"reference": len(pr), "recent": len(pn), "excluded_near_zero_speed": sr + sn})
            if len(pr) < f["min_pairs"] or len(pn) < f["min_pairs"]:
                out["hr_per_speed"] = {**base, **_unavailable(f"too few heart-rate / speed pairs (reference {len(pr)}, recent {len(pn)}; needs {f['min_pairs']})")}
            else:
                rr = statistics.fmean(h for h, _ in pr) / statistics.fmean(v for _, v in pr)
                rn = statistics.fmean(h for h, _ in pn) / statistics.fmean(v for _, v in pn)
                inc = rn / rr - 1
                out["hr_per_speed"] = {**base, "available": True, "measure": {"recent_bpm_per_mps": round(rn, 3), "reference_bpm_per_mps": round(rr, 3), "increase": round(inc, 4)},
                                       "value": round(_clip(100 * inc / f["hundred_at_increase"]), 2)}
    return out


def freshness(sel: dict, T: datetime, prof: dict) -> dict:
    st = prof["streams"]["staleness_s"]
    out = {}
    for name, key in (("wearable", "phys"), ("camera", "track")):
        s = sel[key]
        if not s:
            out[name] = {"fresh": False, "reason": f"no usable {name} sample at the cutoff"}
        else:
            age = (T - s[-1].timestamp).total_seconds()
            out[name] = {"fresh": age <= st, "newest": iso(s[-1].timestamp), "age_s": round(age, 3),
                         "reason": None if age <= st else f"newest {name} sample is {age:.0f} s old (stale after {st:g} s)"}
    return out


def digest(samples: list) -> str:
    h = hashlib.sha256()
    for s in samples:
        h.update(s.model_dump_json().encode()); h.update(b"\n")
    return h.hexdigest()


def score(inp: ScoreInputs, prof: dict, sha: str) -> dict:
    sel = select(inp, prof)
    feats = features(inp, prof, sel)
    fresh = freshness(sel, inp.cutoff, prof)
    cands = {}
    for c in CANDIDATES:
        spec = prof["candidates"][c]
        reasons = []
        for st in spec["streams"]:
            if not fresh[st]["fresh"]:
                reasons.append(f"{st} stream: {fresh[st]['reason']}")
        for f in spec["required"]:
            if not feats.get(f, {}).get("available"):
                reasons.append(f"required feature {f} unavailable: {feats.get(f, {}).get('reason', 'not calculated')}")
        avail = {f: w for f, w in spec["weights"].items() if feats.get(f, {}).get("available")}
        total = sum(spec["weights"].values())
        share = sum(avail.values()) / total
        if share < spec["min_weight_coverage"]:
            reasons.append(f"available features carry {share:.0%} of the weight; {spec['min_weight_coverage']:.0%} needed")
        if c == "combined" and spec.get("requires_camera_alignment") and not inp.camera_alignment:
            reasons.append("no camera clock alignment is stated for this session")
        contrib = [{"feature": f, "weight": round(w / sum(avail.values()), 4), "score": feats[f]["value"],
                    "contribution": round(w / sum(avail.values()) * feats[f]["value"], 2)} for f, w in avail.items()] if avail else []
        left_out = [{"feature": f, "reason": feats.get(f, {}).get("reason", "not calculated")} for f in spec["weights"] if f not in avail]
        value = None if reasons else round(sum(x["contribution"] for x in contrib), 2)
        cands[c] = {"available": value is not None, "value": value, "reasons": reasons, "contributions": contrib if value is not None else [],
                    "features_left_out": left_out, "streams": spec["streams"], "weight_coverage": round(share, 4)}
    return {"athlete_id": inp.athlete_id, "at": iso(inp.cutoff), "knowledge": inp.knowledge, "label": LABEL, "display": DISPLAY,
            "profile": {"version": prof["version"], "sha256": sha, "fictional": prof["fictional"]}, "candidates": cands, "features": feats,
            "streams": fresh, "inputs": {"wearable_samples": len(sel["phys"]), "camera_samples": len(sel["track"]), "excluded": sel["excluded"],
                                         "without_receipt_metadata": sel["without_receipt_metadata"],
                                         "receipt_note": "samples without receipt metadata are assumed available at their own timestamps",
                                         "test_effort_exclusions": [{"start": iso(a), "end": iso(b), "reason": r} for a, b, r in inp.exclusions],
                                         "wearable_sha256": digest(sel["phys"]), "camera_sha256": digest(sel["track"])}}
