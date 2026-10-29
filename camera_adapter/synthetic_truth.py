"""Ground truth for the synthetic camera check: a virtual camera and analytic target trajectories (fictional).

Deliberately independent of the extraction, calibration and estimation code: positions, velocities and tangential
accelerations come from closed-form motion segments, and the image projection comes from a pinhole camera model
(focal length, position, viewing direction), not from a fitted homography. Plain Python.

Field: 40 m x 20 m, x along the near sideline, y across, origin at the near-left corner, metres.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

WIDTH, HEIGHT, FPS, DURATION_S = 960, 540, 20.0, 30.0
FIELD_X, FIELD_Y = 40.0, 20.0


# ---- virtual camera: pinhole, no distortion --------------------------------------------------------------------------------

def _norm(v):
    n = math.sqrt(sum(c * c for c in v))
    return [c / n for c in v]


def _cross(a, b):
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


@dataclass(frozen=True)
class Camera:
    position: tuple = (20.0, -16.0, 13.0)     # metres: 16 m behind the near sideline, 13 m up
    target: tuple = (20.0, 9.0, 0.0)          # looking at a point near the field centre
    focal_px: float = 400.0

    def field_to_image_matrix(self):
        z = _norm([t - p for t, p in zip(self.target, self.position)])
        x = _norm(_cross(z, [0.0, 0.0, 1.0]))
        y = _cross(z, x)
        r = [x, y, z]
        t = [-sum(r[i][j] * self.position[j] for j in range(3)) for i in range(3)]
        k = [[self.focal_px, 0, (WIDTH - 1) / 2], [0, self.focal_px, (HEIGHT - 1) / 2], [0, 0, 1]]
        m = [[r[i][0], r[i][1], t[i]] for i in range(3)]      # ground plane z = 0: columns r1, r2, t
        return [[sum(k[i][a] * m[a][j] for a in range(3)) for j in range(3)] for i in range(3)]

    def project(self, x: float, y: float) -> tuple[float, float]:
        h = self.field_to_image_matrix()
        u = h[0][0] * x + h[0][1] * y + h[0][2]
        v = h[1][0] * x + h[1][1] * y + h[1][2]
        w = h[2][0] * x + h[2][1] * y + h[2][2]
        return u / w, v / w


# ---- trajectories ------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Segment:
    t0: float
    t1: float
    kind: str              # "rest", "line" or "arc"
    start: tuple           # rest/line: start point; arc: centre
    direction: tuple = (1.0, 0.0)   # line: unit direction; arc: (radius, start angle in radians), counter-clockwise
    v0: float = 0.0
    a: float = 0.0

    def state(self, t: float):
        tau = t - self.t0
        if self.kind == "rest":
            return self.start, (0.0, 0.0), 0.0
        s = self.v0 * tau + 0.5 * self.a * tau * tau
        v = self.v0 + self.a * tau
        if self.kind == "line":
            dx, dy = self.direction
            return (self.start[0] + dx * s, self.start[1] + dy * s), (dx * v, dy * v), self.a
        r, phi0 = self.direction
        phi = phi0 + s / r
        return (self.start[0] + r * math.cos(phi), self.start[1] + r * math.sin(phi)), (-math.sin(phi) * v, math.cos(phi) * v), self.a


def _line(t0, t1, start, d, v0, a):
    return Segment(t0, t1, "line", start, d, v0, a)


TARGETS = {
    # red: rest, accelerate 2 m/s2, cruise 4 m/s, decelerate, rest, a slower run across, rest
    "red": [Segment(0, 3, "rest", (5.0, 5.0)), _line(3, 5, (5.0, 5.0), (1, 0), 0, 2), _line(5, 10, (9.0, 5.0), (1, 0), 4, 0), _line(10, 12, (29.0, 5.0), (1, 0), 4, -2),
            Segment(12, 15, "rest", (33.0, 5.0)), _line(15, 16, (33.0, 5.0), (0, 1), 0, 2), _line(16, 20, (33.0, 6.0), (0, 1), 2, 0), _line(20, 21, (33.0, 14.0), (0, 1), 2, -2),
            Segment(21, 30, "rest", (33.0, 15.0))],
    # blue: rest, accelerate, cruise 3 m/s, decelerate, rest, then a curve at 2 m/s (radius 4 m), rest
    "blue": [Segment(0, 2, "rest", (24.0, 12.0)), _line(2, 3.5, (24.0, 12.0), (-1, 0), 0, 2), _line(3.5, 9, (21.75, 12.0), (-1, 0), 3, 0), _line(9, 10.5, (5.25, 12.0), (-1, 0), 3, -2),
             Segment(10.5, 16, "rest", (3.0, 12.0)), Segment(16, 17, "arc", (7.0, 12.0), (4.0, math.pi), 0, 2), Segment(17, 24, "arc", (7.0, 12.0), (4.0, math.pi + 1 / 4), 2, 0),
             Segment(24, 25, "arc", (7.0, 12.0), (4.0, math.pi + 15 / 4), 2, -2), Segment(25, 30, "rest", (7.0 + 4 * math.cos(math.pi + 4.0), 12.0 + 4 * math.sin(math.pi + 4.0)))],
    # yellow: an extra target that appears later and is never assigned to anyone
    "yellow": [_line(18, 24, (10.0, 3.0), (2 / math.sqrt(5), 1 / math.sqrt(5)), 1.0, 0)],
}
# Visibility plan (rendering only; the extractor and the adapter never read it)
OCCLUDER_FIELD = (18.0, 3.2, 20.6, 6.8)              # a grey box drawn over the near part of the field: red passes behind it
DROPPED = {"blue": [(12.0, 12.2), (13.0, 13.6)]}    # blue not drawn: one short dropout (0.2 s) and one long one (0.6 s)
ACCEL_CHANGES = {"red": [3, 5, 10, 12, 15, 16, 20, 21], "blue": [2, 3.5, 9, 10.5, 16, 17, 24, 25]}


def truth(target: str, t: float):
    """(position, velocity, tangential acceleration, speed) at time t, or None when the target does not exist then."""
    for seg in TARGETS[target]:
        if seg.t0 <= t < seg.t1 or (t == seg.t1 == TARGETS[target][-1].t1):
            pos, vel, acc = seg.state(t)
            return pos, vel, acc, math.hypot(*vel)
    return None


def dropped(target: str, t: float) -> bool:
    return any(a <= t < b for a, b in DROPPED.get(target, []))
