"""Gap-aware distance estimate over consecutive tracking samples. Pure function, no I/O.

For each pair of consecutive samples in the selected window, the interval is eligible when
  - elapsed time is positive and no greater than `max_gap_seconds`, and
  - both endpoint `tracking_confidence` values are at least `min_confidence`.
Each eligible interval contributes (speed_prev + speed_cur) / 2 * elapsed (trapezoidal rule), which
assumes speed changes linearly between the two samples. Ineligible intervals are counted once and
contribute nothing: bad samples are not removed and bridged over, and nothing is extrapolated before
the first sample, after the last sample, or across an excluded interval.

The defaults are adjustable engineering values for development, not validated sport-specific standards.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.models.summary import DistanceEstimate
from app.models.tracking import TrackingSample

DEFAULT_MAX_GAP_SECONDS = 3.0
DEFAULT_MIN_CONFIDENCE = 0.5


def estimate_distance(
    samples: Sequence[TrackingSample],
    window_seconds: float,
    max_gap_seconds: float = DEFAULT_MAX_GAP_SECONDS,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> DistanceEstimate:
    """Estimate distance from tracking samples sorted oldest to newest."""
    if max_gap_seconds <= 0:
        raise ValueError("max_gap_seconds must be positive")
    if not 0.0 <= min_confidence <= 1.0:
        raise ValueError("min_confidence must be between 0 and 1")

    distance = 0.0
    included = 0.0
    eligible = 0
    excluded = 0
    for prev, cur in zip(samples, samples[1:]):
        elapsed = (cur.timestamp - prev.timestamp).total_seconds()
        if 0 < elapsed <= max_gap_seconds and prev.tracking_confidence >= min_confidence and cur.tracking_confidence >= min_confidence:
            distance += (prev.speed_mps + cur.speed_mps) / 2 * elapsed
            included += elapsed
            eligible += 1
        else:
            excluded += 1

    fraction = included / window_seconds if window_seconds > 0 else 0.0
    return DistanceEstimate(
        estimated_distance_m=distance if eligible else None,  # 0.0 is a real result when eligible intervals were stationary
        included_duration_seconds=included,
        included_window_fraction=min(fraction, 1.0),
        excluded_interval_count=excluded,
        max_gap_seconds=max_gap_seconds,
        min_confidence=min_confidence,
    )
