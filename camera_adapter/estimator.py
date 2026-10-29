"""Causal movement estimator: field positions over time -> position, speed and signed acceleration at the newest instant.

For each athlete, the estimator keeps the valid observations of the last `window_s` seconds (default 0.5 s) and fits,
per axis, a quadratic in time by least squares, evaluated at the NEWEST observation's time:
    x(tau) = a + b*tau + c*tau^2   with tau = t_i - t_now <= 0
    position = a, velocity = b, acceleration vector = 2c.
Only observations at or before t_now are used (causal: an online estimate never sees the future). The fit smooths
measurement noise over the window; for motion whose acceleration changes inside the window it lags and overshoots
briefly (a boundary effect lasting about one window), which the synthetic check measures.

  speed_mps           = |velocity| (never negative; a standing target gives a legitimate value near 0)
  acceleration_mps2   = tangential acceleration, d|v|/dt = (v . a) / |v|, negative when slowing down. Below
                        `min_speed_for_direction_mps` the direction of travel is undefined; speed can then only grow,
                        so +|a| is reported (correct when starting from rest; near a stop it can show a positive value
                        while the target settles, a documented boundary effect).

No estimate is made (the sample is skipped with a reason) while warming up (fewer than `min_points` observations or
less than `min_span_s` covered in the window), when the fit residual exceeds `max_fit_rms_m`, or within the output-rate
limit. History is RESET, never bridged, when consecutive observations are more than `max_gap_s` apart or when the
athlete's temporary track id changes (a new track is not assumed continuous with the old one); the estimator then
warms up again. Nothing is interpolated or upsampled: every output is at the time of a real observation.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

from camera_adapter.calibration import solve


@dataclass(frozen=True)
class EstimatorConfig:
    window_s: float = 0.5
    min_points: int = 5
    min_span_s: float = 0.3
    max_gap_s: float = 0.3
    min_speed_for_direction_mps: float = 0.3
    max_fit_rms_m: float = 0.3

    def validate(self) -> list[str]:
        e = []
        if not (0 < self.window_s <= 5):
            e.append("window_s must be in (0, 5]")
        if not (isinstance(self.min_points, int) and self.min_points >= 4):
            e.append("min_points must be an integer >= 4 (a quadratic needs more points than unknowns)")
        if not (0 < self.min_span_s <= self.window_s):
            e.append("min_span_s must be in (0, window_s]")
        if not (0 < self.max_gap_s <= self.window_s):
            e.append("max_gap_s must be in (0, window_s]")
        if not (self.min_speed_for_direction_mps >= 0 and self.max_fit_rms_m > 0):
            e.append("min_speed_for_direction_mps must be >= 0 and max_fit_rms_m > 0")
        return e


@dataclass(frozen=True)
class Point:
    t: float
    x: float
    y: float
    confidence: float
    track_id: str


@dataclass(frozen=True)
class Estimate:
    t: float
    x: float
    y: float
    speed_mps: float
    acceleration_mps2: float
    confidence: float
    points: int
    rms_m: float


def quadratic_fit(taus: list[float], values: list[float]) -> tuple[float, float, float]:
    s = [sum(t ** k for t in taus) for k in range(5)]
    r = [sum(v * t ** k for t, v in zip(taus, values)) for k in range(3)]
    return tuple(solve([[s[0], s[1], s[2]], [s[1], s[2], s[3]], [s[2], s[3], s[4]]], r))


@dataclass
class AthleteEstimator:
    config: EstimatorConfig
    history: deque = field(default_factory=deque)

    def add(self, p: Point) -> str | None:
        """Add an observation; returns a reset reason ("gap" or "track_change") when the history was restarted."""
        reason = None
        if self.history:
            last = self.history[-1]
            if p.t - last.t > self.config.max_gap_s + 1e-9:
                reason = "gap"
            elif p.track_id != last.track_id:
                reason = "track_change"
            if reason:
                self.history.clear()
        self.history.append(p)
        while self.history and self.history[0].t < p.t - self.config.window_s - 1e-9:
            self.history.popleft()
        return reason

    def estimate(self) -> tuple[Estimate | None, str | None]:
        """Estimate at the newest observation's time, or (None, reason)."""
        h = list(self.history)
        if len(h) < self.config.min_points or h[-1].t - h[0].t < self.config.min_span_s - 1e-9:
            return None, "warm_up"
        t_now = h[-1].t
        taus = [p.t - t_now for p in h]
        try:
            ax, bx, cx = quadratic_fit(taus, [p.x for p in h])
            ay, by, cy = quadratic_fit(taus, [p.y for p in h])
        except Exception:  # noqa: BLE001  degenerate timing (should not happen with min_span_s > 0)
            return None, "degenerate_fit"
        rms = math.sqrt(sum((ax + bx * t + cx * t * t - p.x) ** 2 + (ay + by * t + cy * t * t - p.y) ** 2 for t, p in zip(taus, h)) / len(h))
        if rms > self.config.max_fit_rms_m:
            return None, "fit_residual"
        speed = math.hypot(bx, by)
        accx, accy = 2 * cx, 2 * cy
        if speed >= self.config.min_speed_for_direction_mps and speed > 0:
            accel = (bx * accx + by * accy) / speed
        else:
            accel = math.hypot(accx, accy)
        return Estimate(t_now, ax, ay, speed, accel, min(p.confidence for p in h), len(h), rms), None
