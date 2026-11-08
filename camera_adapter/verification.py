"""Camera verification against INDEPENDENT references: measured course distance, timed average speed, manually
labelled events (pure functions; scripts/camera_verify.py does the I/O).

INPUTS (format "athletrix-camera-verification", version 1): `fixture_kind` ("synthetic" or "approved_local": stated by
whoever made it, and printed on every report), tracking samples (engine interchange JSON Lines), and any of:
  course_runs        [{athlete_id, start, end, measured_distance_m, reference}]  a run along a tape-measured course
  speed_references   [{athlete_id, start, end, distance_m, time_s, reference}]   independently timed: the reference is the
                     AVERAGE speed over the interval (distance / time), never an instantaneous top speed
  peak_speed_references [{athlete_id, start, end, peak_mps, reference}]         optional; without one the peak-speed
                     check is reported as UNMEASURED
  uncertain_identity [{athlete_id, start, end}]                                  intervals left out (identity not confirmed)
  event_windows      [{athlete_id, start, end}] with manual_events [{kind: sprint|jump|tackle, athlete_id, t, labeller}]
ENGINE SIDE: distance = sum of speed x elapsed time between consecutive samples, never across a gap longer than
max_gap_s, a segment change or an uncertain-identity interval; tracked share = covered seconds / interval seconds.
EVENTS: sprints from app/core/sprints.py; a detected sprint matches a manual sprint label when the label lies within the
sprint's interval widened by match_tolerance_s (each label and each detection matches at most once). Jumps and tackles:
automatic detection is UNAVAILABLE in this version (no rule separates them on camera position and speed alone), so only
the manual counts are reported. When the manual count is 0, detections are listed as false detections; nothing is
divided by zero. TARGETS from the plan are shown as targets: 10 % for distance and speed, 15 % for event counts.
"""

from __future__ import annotations

from datetime import datetime

from app.core.sprints import SprintRule, detect

TARGETS = {"distance_pct": 10.0, "speed_pct": 10.0, "event_count_pct": 15.0}


def t_(v: str) -> datetime:
    return datetime.fromisoformat(v.replace("Z", "+00:00"))


def _in(s, ivals, aid):
    return any(i["athlete_id"] == aid and t_(i["start"]) <= s.timestamp <= t_(i["end"]) for i in ivals)


def engine_distance(samples: list, start: datetime, end: datetime, uncertain: list, aid: str, max_gap_s: float) -> dict:
    pts = sorted((s for s in samples if s.athlete_id == aid and start <= s.timestamp <= end), key=lambda s: s.timestamp)
    excl_identity = sum(1 for s in pts if _in(s, uncertain, aid))
    pts = [s for s in pts if not _in(s, uncertain, aid)]
    dist = cov = 0.0
    gaps = seg = 0
    for a, b in zip(pts, pts[1:]):
        dt = (b.timestamp - a.timestamp).total_seconds()
        if dt > max_gap_s:
            gaps += 1
            continue
        if a.segment_id != b.segment_id:
            seg += 1
            continue
        dist += (a.speed_mps + b.speed_mps) / 2 * dt
        cov += dt
    span = (end - start).total_seconds()
    return {"distance_m": round(dist, 3), "covered_s": round(cov, 3), "interval_s": span, "tracked_share": round(cov / span, 4) if span > 0 else None,
            "excluded": {"gaps_not_bridged": gaps, "segment_changes_not_bridged": seg, "samples_with_uncertain_identity": excl_identity}}


def pct(err: float, ref: float) -> float | None:
    return None if not ref else round(100 * err / ref, 2)


def verify(doc: dict, samples: list, max_gap_s: float = 0.5, rule: SprintRule = SprintRule(), match_tolerance_s: float = 1.0) -> dict:
    unc = doc.get("uncertain_identity", [])
    out = {"fixture_kind": doc["fixture_kind"], "targets": TARGETS, "course_runs": [], "speed": [], "peak_speed": [], "events": []}
    for c in doc.get("course_runs", []):
        e = engine_distance(samples, t_(c["start"]), t_(c["end"]), unc, c["athlete_id"], max_gap_s)
        err = pct(e["distance_m"] - c["measured_distance_m"], c["measured_distance_m"])
        out["course_runs"].append({**c, "engine": e, "error_pct": err, "within_target": None if err is None else abs(err) <= TARGETS["distance_pct"],
                                   "note": "a partly tracked run gives a short distance: read the tracked share next to the error"})
    for s in doc.get("speed_references", []):
        a, b = t_(s["start"]), t_(s["end"])
        e = engine_distance(samples, a, b, unc, s["athlete_id"], max_gap_s)
        ref = s["distance_m"] / s["time_s"]
        eng = e["distance_m"] / e["covered_s"] if e["covered_s"] else None
        err = None if eng is None else pct(eng - ref, ref)
        out["speed"].append({**s, "reference_average_speed_mps": round(ref, 4), "engine_average_speed_mps": None if eng is None else round(eng, 4), "tracked_share": e["tracked_share"],
                             "error_pct": err, "within_target": None if err is None else abs(err) <= TARGETS["speed_pct"],
                             "meaning": "average speed over the timed interval; a timed 20 m sprint is not ground truth for instantaneous top speed"})
    peaks = doc.get("peak_speed_references", [])
    if not peaks:
        out["peak_speed"] = [{"status": "UNMEASURED", "reason": "no independent peak-speed reference was supplied"}]
    for p in peaks:
        pts = [s.speed_mps for s in samples if s.athlete_id == p["athlete_id"] and t_(p["start"]) <= s.timestamp <= t_(p["end"]) and not _in(s, unc, p["athlete_id"])]
        eng = max(pts) if pts else None
        err = None if eng is None else pct(eng - p["peak_mps"], p["peak_mps"])
        out["peak_speed"].append({**p, "engine_peak_mps": eng, "error_pct": err, "within_target": None if err is None else abs(err) <= TARGETS["speed_pct"]})
    labels = doc.get("manual_events", [])
    for w in doc.get("event_windows", []):
        aid, a, b = w["athlete_id"], t_(w["start"]), t_(w["end"])
        mine = [s for s in samples if s.athlete_id == aid and a <= s.timestamp <= b and not _in(s, unc, aid)]
        det = detect(mine, rule)
        man = [x for x in labels if x["athlete_id"] == aid and a <= t_(x["t"]) <= b]
        sprints = [x for x in man if x["kind"] == "sprint"]
        used, matched = set(), 0
        for d in det:
            for i, x in enumerate(sprints):
                if i in used:
                    continue
                if (d["start"] - t_(x["t"])).total_seconds() <= match_tolerance_s and (t_(x["t"]) - d["end"]).total_seconds() <= match_tolerance_s:
                    used.add(i)
                    matched += 1
                    d["matched_label"] = x["t"]
                    break
        false = [{"start": d["start"].isoformat(), "end": d["end"].isoformat(), "peak_speed_mps": round(d["peak_speed_mps"], 2)} for d in det if "matched_label" not in d]
        ev = {"athlete_id": aid, "window": {"start": w["start"], "end": w["end"]}, "sprint": {"manual": len(sprints), "detected": len(det), "matched": matched,
              "missed": len(sprints) - matched, "false_detections": false, "rule": vars(rule)}}
        if sprints:
            err = pct(len(det) - len(sprints), len(sprints))
            ev["sprint"].update(count_error_pct=err, within_target=abs(err) <= TARGETS["event_count_pct"])
        else:
            ev["sprint"].update(count_error_pct=None, within_target=None, note=f"no manual sprint label: {len(false)} false detection(s) shown directly")
        for k in ("jump", "tackle"):
            ev[k] = {"manual": sum(1 for x in man if x["kind"] == k), "automatic": "UNAVAILABLE: no validated rule separates this event on camera position and speed"}
        out["events"].append(ev)
    return out
