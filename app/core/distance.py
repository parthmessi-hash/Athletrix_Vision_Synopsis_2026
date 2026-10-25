"""Gap-aware distance and high-speed estimates over consecutive tracking samples. Pure functions, no I/O.

Eligibility (shared by every estimate here): for each pair of consecutive samples in the selected
window, the interval is eligible when
  - elapsed time is positive and no greater than `max_gap_seconds`, and
  - both endpoint `tracking_confidence` values are at least `min_confidence`.
Within an eligible interval speed is assumed to change linearly between the two samples, so distance
is the trapezoid (v_prev + v_cur) / 2 * elapsed. Ineligible intervals are counted once and contribute
nothing: bad samples are not removed and bridged over, and nothing is extrapolated before the first
sample, after the last sample, or across an excluded interval.

The high-speed estimate uses exactly the same eligible intervals and the same linear assumption, and
counts only the part of each interval where speed is at or above the threshold; when an interval
crosses the threshold, the crossing time is interpolated and only the qualifying part is integrated.

All defaults are adjustable engineering values for development, not validated sport-specific or
athlete-specific standards.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import NamedTuple

from app.models.summary import DistanceEstimate, HighSpeedEstimate
from app.models.tracking import TrackingSample

DEFAULT_MAX_GAP_SECONDS = 3.0
DEFAULT_MIN_CONFIDENCE = 0.5
DEFAULT_HIGH_SPEED_THRESHOLD_MPS = 5.0


class Interval(NamedTuple):
    v0: float        # speed at the start, m/s
    v1: float        # speed at the end, m/s
    elapsed: float   # seconds


def _validate_rules(max_gap_seconds: float, min_confidence: float) -> None:
    if not (math.isfinite(max_gap_seconds) and max_gap_seconds > 0):
        raise ValueError("max_gap_seconds must be a positive finite number")
    if not (math.isfinite(min_confidence) and 0.0 <= min_confidence <= 1.0):
        raise ValueError("min_confidence must be between 0 and 1")


def eligible_intervals(
    samples: Sequence[TrackingSample],
    max_gap_seconds: float = DEFAULT_MAX_GAP_SECONDS,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> tuple[list[Interval], int]:
    """Split consecutive samples (sorted oldest to newest) into eligible intervals; return them and the excluded count."""
    _validate_rules(max_gap_seconds, min_confidence)
    eligible: list[Interval] = []
    excluded = 0
    for prev, cur in zip(samples, samples[1:]):
        elapsed = (cur.timestamp - prev.timestamp).total_seconds()
        if 0 < elapsed <= max_gap_seconds and prev.tracking_confidence >= min_confidence and cur.tracking_confidence >= min_confidence:
            eligible.append(Interval(prev.speed_mps, cur.speed_mps, elapsed))
        else:
            excluded += 1
    return eligible, excluded


def interval_distance(iv: Interval) -> float:
    """Trapezoidal distance of one interval, metres."""
    return (iv.v0 + iv.v1) / 2 * iv.elapsed


def high_speed_portion(iv: Interval, threshold_mps: float) -> tuple[float, float]:
    """(seconds, metres) of the interval spent at or above the threshold, assuming linear speed change.

    Fully above (or constantly at) the threshold: the whole interval. Fully below: nothing. A crossing
    is interpolated at t_c = elapsed * (threshold - v0) / (v1 - v0) and only the qualifying side counts.
    An interval that merely touches the threshold at one endpoint contributes zero.
    """
    v0, v1, T = iv
    above0, above1 = v0 >= threshold_mps, v1 >= threshold_mps
    if above0 and above1:
        return T, interval_distance(iv)
    if not above0 and not above1:
        return 0.0, 0.0
    t_c = T * (threshold_mps - v0) / (v1 - v0)
    if above1:  # rising: qualifies from the crossing to the end
        dur = T - t_c
        return dur, (threshold_mps + v1) / 2 * dur
    return t_c, (v0 + threshold_mps) / 2 * t_c  # falling: qualifies from the start to the crossing


def estimate_distance(
    samples: Sequence[TrackingSample],
    window_seconds: float,
    max_gap_seconds: float = DEFAULT_MAX_GAP_SECONDS,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> DistanceEstimate:
    """Estimate distance from tracking samples sorted oldest to newest."""
    intervals, excluded = eligible_intervals(samples, max_gap_seconds, min_confidence)
    distance = sum(interval_distance(iv) for iv in intervals)
    included = sum(iv.elapsed for iv in intervals)
    fraction = included / window_seconds if window_seconds > 0 else 0.0
    return DistanceEstimate(
        estimated_distance_m=distance if intervals else None,  # 0.0 is a real result when eligible intervals were stationary
        included_duration_seconds=included,
        included_window_fraction=min(fraction, 1.0),
        excluded_interval_count=excluded,
        max_gap_seconds=max_gap_seconds,
        min_confidence=min_confidence,
    )


def estimate_high_speed(
    samples: Sequence[TrackingSample],
    threshold_mps: float = DEFAULT_HIGH_SPEED_THRESHOLD_MPS,
    max_gap_seconds: float = DEFAULT_MAX_GAP_SECONDS,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> HighSpeedEstimate:
    """Time and distance at or above `threshold_mps`, over the same eligible intervals as estimate_distance."""
    if not (math.isfinite(threshold_mps) and threshold_mps > 0):
        raise ValueError("threshold_mps must be a positive finite number")
    intervals, _ = eligible_intervals(samples, max_gap_seconds, min_confidence)
    if not intervals:
        return HighSpeedEstimate(high_speed_duration_seconds=None, high_speed_distance_m=None, high_speed_threshold_mps=threshold_mps)
    duration = 0.0
    distance = 0.0
    for iv in intervals:
        d, m = high_speed_portion(iv, threshold_mps)
        duration += d
        distance += m
    # By construction these cannot exceed the eligible totals; clamp only to absorb floating-point noise.
    duration = min(duration, sum(iv.elapsed for iv in intervals))
    distance = min(distance, sum(interval_distance(iv) for iv in intervals))
    return HighSpeedEstimate(high_speed_duration_seconds=duration, high_speed_distance_m=distance, high_speed_threshold_mps=threshold_mps)
