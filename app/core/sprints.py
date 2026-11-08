"""Sprint detection from camera-derived tracking samples (pure function).

A sprint STARTS at the first of `min_samples` consecutive valid samples at or above `threshold_mps` and ENDS at the
last valid sample before the speed stays below `threshold_mps - exit_hysteresis_mps` for `exit_samples` consecutive
samples, or immediately at a break. Valid samples: value_basis not "predicted", confidence at least `min_confidence`.
BREAKS end a sprint (or a candidate) without bridging: a time gap longer than `max_gap_s` between consecutive samples,
or a change of segment_id (a continuity break or a re-confirmed identity: never one person's sprint across two
segments). Noise near the threshold: a dip shorter than `exit_samples` does not split a sprint, and fewer than
`min_samples` samples above the threshold never start one. The detector reports peak and mean speed, duration and
covered distance (speed x elapsed time between samples) of each sprint. Rules are configurable engineering defaults
(the 5.5 m/s threshold is the draft plan's proposal), not validated sport standards.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SprintRule:
    threshold_mps: float = 5.5
    min_samples: int = 3
    exit_hysteresis_mps: float = 0.3
    exit_samples: int = 2
    max_gap_s: float = 0.5
    min_confidence: float = 0.5


def detect(samples: list, rule: SprintRule = SprintRule()) -> list[dict]:
    """samples: TrackingSample-like objects of ONE athlete, any order. Returns sprints oldest first."""
    pts = sorted((s for s in samples if (getattr(s, "value_basis", None) or "observed") != "predicted" and s.tracking_confidence >= rule.min_confidence),
                 key=lambda s: s.timestamp)
    out, cur, above, below, prev = [], None, [], 0, None

    def close(reason):
        nonlocal cur
        if cur and len(cur["pts"]) >= rule.min_samples:
            p = cur["pts"]
            dist = sum((a.speed_mps + b.speed_mps) / 2 * (b.timestamp - a.timestamp).total_seconds() for a, b in zip(p, p[1:]))
            out.append({"start": p[0].timestamp, "end": p[-1].timestamp, "samples": len(p), "peak_speed_mps": max(x.speed_mps for x in p),
                        "mean_speed_mps": sum(x.speed_mps for x in p) / len(p), "duration_s": (p[-1].timestamp - p[0].timestamp).total_seconds(),
                        "distance_m": dist, "segment_id": p[0].segment_id, "ended_by": reason})
        cur = None

    for s in pts:
        broke = prev is not None and ((s.timestamp - prev.timestamp).total_seconds() > rule.max_gap_s or s.segment_id != prev.segment_id)
        if broke:
            close("gap" if (s.timestamp - prev.timestamp).total_seconds() > rule.max_gap_s else "segment_change")
            above, below = [], 0
        prev = s
        if cur is None:
            if s.speed_mps >= rule.threshold_mps:
                above.append(s)
                if len(above) >= rule.min_samples:
                    cur, below = {"pts": list(above)}, 0
                    above = []
            else:
                above = []
            continue
        if s.speed_mps < rule.threshold_mps - rule.exit_hysteresis_mps:
            below += 1
            if below >= rule.exit_samples:
                close("speed_below_threshold")
                above, below = [], 0
            continue
        below = 0
        cur["pts"].append(s)
    close("end_of_data")
    return out
