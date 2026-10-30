"""Synthetic people scene for the real person detector (needs OpenCV and NumPy): drawn fictional figures, analytic truth.

A virtual pinhole camera (no lens distortion) looks across a fictional 24 m x 14 m practice area from behind the near
sideline. Five cartoon figures, drawn by this code (no footage of any person), move along closed-form paths:
  red     rests, accelerates to 4 m/s along the near side, cruises, decelerates, rests (the reference straight run)
  blue    runs the other way 2.5 m farther from the camera and passes behind red in the image at about 5.6-5.9 s
          (an identity challenge for the tracker)
  yellow  jogs along the far side behind a 0.9 m sideline board that hides its legs for part of the run
          (the box bottom is then not the feet: a known ground-point error)
  white   walks out of the right edge of the view and comes back later (a track loss and re-entry)
  black   a referee-like figure that is never assigned to anyone
Ground truth is each figure's foot position (x, y) on the field, its velocity and tangential acceleration at any time.
The renderer draws a figure's height from the projection of its head (1.75 m) and scales its width with it. This is
a synthetic test of the pipeline's plumbing and geometry, not evidence about real players, kits, lighting or crowds.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from camera_adapter.synthetic_truth import Segment, _cross, _line, _norm

WIDTH, HEIGHT, FPS, DURATION_S = 1280, 720, 25.0, 20.0
FIELD_X, FIELD_Y = 24.0, 14.0
FIGURE_HEIGHT_M = 1.75
BOARD = {"x0": 15.0, "x1": 21.0, "y": 11.6, "height_m": 0.9}     # sideline board in front of yellow's path (far side)


@dataclass(frozen=True)
class Camera3:
    position: tuple = (12.0, -9.0, 6.5)
    target: tuple = (12.0, 7.0, 0.0)
    focal_px: float = 820.0

    def matrix(self):
        z = _norm([t - p for t, p in zip(self.target, self.position)])
        x = _norm(_cross(z, [0.0, 0.0, 1.0]))
        y = _cross(z, x)
        r = [x, y, z]
        t = [-sum(r[i][j] * self.position[j] for j in range(3)) for i in range(3)]
        k = [[self.focal_px, 0, (WIDTH - 1) / 2], [0, self.focal_px, (HEIGHT - 1) / 2], [0, 0, 1]]
        rt = [[r[i][0], r[i][1], r[i][2], t[i]] for i in range(3)]
        return [[sum(k[i][a] * rt[a][j] for a in range(3)) for j in range(4)] for i in range(3)]

    def project3(self, x: float, y: float, z: float = 0.0) -> tuple[float, float]:
        p = self.matrix()
        u, v, w = (p[i][0] * x + p[i][1] * y + p[i][2] * z + p[i][3] for i in range(3))
        return u / w, v / w

    def project(self, x: float, y: float) -> tuple[float, float]:
        return self.project3(x, y, 0.0)


FIGURES = {
    "red":    [Segment(0, 2, "rest", (4.0, 3.0)), _line(2, 4, (4.0, 3.0), (1, 0), 0, 2), _line(4, 6, (8.0, 3.0), (1, 0), 4, 0), _line(6, 8, (16.0, 3.0), (1, 0), 4, -2),
               Segment(8, 20, "rest", (20.0, 3.0))],
    "blue":   [Segment(0, 2, "rest", (24.0, 5.5)), _line(2, 3, (24.0, 5.5), (-1, 0), 0, 3), _line(3, 8, (22.5, 5.5), (-1, 0), 3, 0), _line(8, 9, (7.5, 5.5), (-1, 0), 3, -3),
               Segment(9, 20, "rest", (6.0, 5.5))],
    "yellow": [Segment(0, 1, "rest", (11.0, 12.6)), _line(1, 2, (11.0, 12.6), (1, 0), 0, 2.5), _line(2, 6.5, (12.25, 12.6), (1, 0), 2.5, 0), _line(6.5, 7.5, (23.5, 12.6), (1, 0), 2.5, -2.5),
               Segment(7.5, 20, "rest", (24.75, 12.6))],
    "white":  [Segment(0, 1, "rest", (18.0, 7.5)), _line(1, 9, (18.0, 7.5), (1, 0), 1.5, 0), _line(9, 17, (30.0, 7.5), (-1, 0), 1.5, 0), Segment(17, 20, "rest", (18.0, 7.5))],
    "black":  [Segment(0, 2, "rest", (8.0, 9.0)), _line(2, 12, (8.0, 9.0), (0.8, -0.6), 1.0, 0), _line(12, 20, (16.0, 3.0), (-0.8, 0.6), 1.0, 0)],
}
SHIRTS_BGR = {"red": (40, 40, 210), "blue": (200, 70, 30), "yellow": (30, 200, 230), "white": (235, 235, 235), "black": (25, 25, 25)}
SPEED_CHANGES = {"red": [2, 4, 6, 8], "blue": [2, 3, 8, 9], "yellow": [1, 2, 6.5, 7.5], "white": [1, 9, 17], "black": [2, 12]}


def truth(name: str, t: float):
    """(foot position, velocity, tangential acceleration, speed) at time t."""
    segs = FIGURES[name]
    for seg in segs:
        if seg.t0 <= t < seg.t1 or (t == seg.t1 == segs[-1].t1):
            pos, vel, acc = seg.state(t)
            return pos, vel, acc, math.hypot(*vel)
    return None


def in_view(camera: Camera3, name: str, t: float, margin_px: float = 0.0) -> bool:
    """Whether the figure's foot point is inside the image (the renderer may still clip the body)."""
    st = truth(name, t)
    if st is None:
        return False
    u, v = camera.project(*st[0])
    return margin_px <= u <= WIDTH - 1 - margin_px and 0 <= v <= HEIGHT - 1


def legs_hidden(name: str, t: float) -> bool:
    """True while the figure stands directly behind the sideline board (truth for the report; never read by the pipeline)."""
    st = truth(name, t)
    return st is not None and BOARD["x0"] <= st[0][0] <= BOARD["x1"] and st[0][1] > BOARD["y"]


def _draw_figure(img, cv2, np, u_foot, v_foot, h_px, shirt, phase):
    s = h_px / 180.0
    def P(dx, dy):
        return (int(round(u_foot + dx * s)), int(round(v_foot - dy * s)))
    skin, shorts, sock = (140, 170, 215), (40, 40, 40), (240, 240, 240)
    swing = math.sin(phase) * 14
    cv2.ellipse(img, P(0, 165), (max(1, int(11 * s)), max(1, int(13 * s))), 0, 0, 360, skin, -1, cv2.LINE_AA)
    cv2.ellipse(img, P(0, 170), (max(1, int(11 * s)), max(1, int(8 * s))), 0, 180, 360, (30, 30, 60), -1, cv2.LINE_AA)
    cv2.rectangle(img, P(-4, 152), P(4, 146), skin, -1)
    cv2.fillPoly(img, [np.array([P(-20, 148), P(20, 148), P(17, 95), P(-17, 95)], np.int32)], shirt, cv2.LINE_AA)
    th = max(1, int(8 * s))
    cv2.line(img, P(-20, 145), P(-28 - swing * 0.4, 110), shirt, th, cv2.LINE_AA)
    cv2.line(img, P(-28 - swing * 0.4, 110), P(-31 - swing * 0.6, 82), skin, max(1, int(6 * s)), cv2.LINE_AA)
    cv2.line(img, P(20, 145), P(28 + swing * 0.4, 110), shirt, th, cv2.LINE_AA)
    cv2.line(img, P(28 + swing * 0.4, 110), P(31 + swing * 0.6, 82), skin, max(1, int(6 * s)), cv2.LINE_AA)
    cv2.fillPoly(img, [np.array([P(-17, 96), P(17, 96), P(18, 72), P(-18, 72)], np.int32)], shorts, cv2.LINE_AA)
    lt = max(1, int(10 * s))
    for side, sw in ((-1, swing), (1, -swing)):
        knee = P(side * 9 + sw * 0.6, 40)
        foot = P(side * 10 + sw, 4)
        cv2.line(img, P(side * 9, 74), knee, skin, lt, cv2.LINE_AA)
        cv2.line(img, knee, foot, sock, max(1, int(8 * s)), cv2.LINE_AA)
        cv2.ellipse(img, (foot[0] + side * max(1, int(3 * s)), foot[1]), (max(1, int(8 * s)), max(1, int(3 * s))), 0, 0, 360, (20, 20, 20), -1, cv2.LINE_AA)


def render(path, camera: Camera3 | None = None) -> dict:
    """Write the scene as an MJPG AVI at a constant frame rate; return per-frame drawing facts for the report only."""
    import cv2  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415
    camera = camera or Camera3()
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), FPS, (WIDTH, HEIGHT))
    if not writer.isOpened():
        raise RuntimeError("OpenCV could not open an MJPG video writer")
    base = np.zeros((HEIGHT, WIDTH, 3), np.uint8)
    base[:] = (55, 115, 55)
    for i in range(0, HEIGHT, 60):   # mowing stripes, for texture
        base[i:i + 30] = (60, 124, 60)
    def line(a, b):
        cv2.line(base, tuple(int(round(c)) for c in camera.project(*a)), tuple(int(round(c)) for c in camera.project(*b)), (225, 225, 225), 2, cv2.LINE_AA)
    for a, b in (((0, 0), (FIELD_X, 0)), ((FIELD_X, 0), (FIELD_X, FIELD_Y)), ((FIELD_X, FIELD_Y), (0, FIELD_Y)), ((0, FIELD_Y), (0, 0)), ((FIELD_X / 2, 0), (FIELD_X / 2, FIELD_Y))):
        line(a, b)
    board = np.array([camera.project3(BOARD["x0"], BOARD["y"], 0), camera.project3(BOARD["x1"], BOARD["y"], 0),
                      camera.project3(BOARD["x1"], BOARD["y"], BOARD["height_m"]), camera.project3(BOARD["x0"], BOARD["y"], BOARD["height_m"])])
    n = int(round(DURATION_S * FPS))
    drawn = {}
    for f in range(n):
        t = f / FPS
        frame = base.copy()
        order = []
        for name in FIGURES:
            st = truth(name, t)
            if st is None:
                continue
            (x, y), _, _, speed = st
            u, v = camera.project3(x, y, 0)
            uh, vh = camera.project3(x, y, FIGURE_HEIGHT_M)
            dist = math.dist((x, y, 0), camera.position)
            order.append((-dist, name, u, v, v - vh, speed))
        drawn[f] = {}
        for _, name, u, v, h, speed in sorted(order):   # far figures first, near ones over them
            phase = 2 * math.pi * 1.4 * t * min(1.0, speed / 2.0) + hash(name) % 7
            if speed < 0.05:
                phase = 0.0
            _draw_figure(frame, cv2, np, u, v, h, SHIRTS_BGR[name], phase)
            drawn[f][name] = (u, v, h)
        cv2.fillPoly(frame, [np.round(board).astype(np.int32)], (40, 40, 160), cv2.LINE_AA)
        cv2.putText(frame, "FICTIONAL", tuple(int(c) for c in board[3] + (8, 22)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2, cv2.LINE_AA)
        writer.write(frame)
    writer.release()
    return {"frames": n, "drawn": drawn}
