"""Synthetic video and SYNTHETIC MARKER TRACKING (needs OpenCV and NumPy: pip install -r requirements-vision.txt).

render_video  draws the fictional field (lines, calibration crosses), coloured disc markers at the ground-truth
              image positions (camera_adapter/synthetic_truth.py), a grey occluder and deliberately dropped frames,
              and writes an MJPG AVI at a constant frame rate.
extract       finds each colour's disc in every decoded frame by fixed colour thresholds and reports its centroid,
              a score (visible fraction of the disc area) and a temporary track id. This is colour-blob tracking of
              synthetic markers: it is NOT person detection, it cannot identify players, and the colour is not used
              as identity (tracks are assigned to athletes only through an explicit assignment file).
"""

from __future__ import annotations

import math
from pathlib import Path

import cv2
import numpy as np

from camera_adapter import synthetic_truth as st
from camera_adapter.observations import Observation, ObservationFile, Video

MARKER_RADIUS_PX = 7
MARKER_AREA_PX = math.pi * MARKER_RADIUS_PX ** 2
COLOURS_BGR = {"red": (40, 40, 230), "blue": (230, 60, 40), "yellow": (40, 220, 235)}
BACKGROUND_BGR = (55, 95, 55)
SHIFT = 4   # sub-pixel drawing: coordinates scaled by 2**SHIFT


def _px(p):
    return int(round(p[0] * (1 << SHIFT))), int(round(p[1] * (1 << SHIFT)))


def occluder_polygon(camera: st.Camera):
    x0, y0, x1, y1 = st.OCCLUDER_FIELD
    return np.array([camera.project(x, y) for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))], dtype=np.float64)


def render_video(path: Path, camera: st.Camera | None = None) -> dict:
    """Write the synthetic video; return what was drawn per frame (for the report only, never for extraction)."""
    camera = camera or st.Camera()
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), st.FPS, (st.WIDTH, st.HEIGHT))
    if not writer.isOpened():
        raise RuntimeError("OpenCV could not open an MJPG video writer")
    base = np.zeros((st.HEIGHT, st.WIDTH, 3), np.uint8)
    base[:] = BACKGROUND_BGR
    lines = [((0, 0), (40, 0)), ((40, 0), (40, 20)), ((40, 20), (0, 20)), ((0, 20), (0, 0)), ((20, 0), (20, 20))]
    for a, b in lines:
        cv2.line(base, _px(camera.project(*a)), _px(camera.project(*b)), (225, 225, 225), 1, cv2.LINE_AA, SHIFT)
    occ = occluder_polygon(camera)
    drawn = {}
    n = int(round(st.DURATION_S * st.FPS))
    for f in range(n):
        t = f / st.FPS
        frame = base.copy()
        drawn[f] = {}
        for name in ("red", "blue", "yellow"):
            state = st.truth(name, t)
            if state is None or st.dropped(name, t):
                continue
            u, v = camera.project(*state[0])
            cv2.circle(frame, _px((u, v)), MARKER_RADIUS_PX << SHIFT, COLOURS_BGR[name], -1, cv2.LINE_AA, SHIFT)
            drawn[f][name] = (u, v)
        cv2.fillPoly(frame, [np.round(occ * (1 << SHIFT)).astype(np.int32)], (128, 128, 128), cv2.LINE_AA, SHIFT)
        writer.write(frame)
    writer.release()
    return {"frames": n, "drawn": drawn}


def _mask(frame, name):
    b, g, r = (frame[:, :, i].astype(np.int16) for i in range(3))
    if name == "red":
        m = (r > 150) & (g < 110) & (b < 110)
    elif name == "blue":
        m = (b > 150) & (r < 110) & (g < 130)
    else:
        m = (r > 150) & (g > 150) & (b < 120)
    return m.astype(np.uint8)


def extract(video_path: Path, *, max_missing_frames: int = 5, max_jump_px: float = 40.0, min_area_px: int = 6) -> tuple[ObservationFile, dict]:
    """Synthetic marker tracking over a decoded video. Returns the observations and track_id -> colour (for the
    operator's assignment step and the report; the adapter never sees colours)."""
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open {video_path}")
    fps = cap.get(cv2.CAP_PROP_FPS)
    width, height = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    obs: list[Observation] = []
    tracks: dict[str, dict] = {}       # colour -> {"id", "last_frame", "last_uv"}
    colour_of: dict[str, str] = {}
    next_id = 1
    f = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        for name in COLOURS_BGR:
            n, labels, stats, cents = cv2.connectedComponentsWithStats(_mask(frame, name), connectivity=8)
            if n <= 1:
                continue
            best = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
            area = int(stats[best, cv2.CC_STAT_AREA])
            if area < min_area_px:
                continue
            u, v = float(cents[best][0]), float(cents[best][1])
            tr = tracks.get(name)
            if tr is None or f - tr["last_frame"] > max_missing_frames + 1 or math.dist((u, v), tr["last_uv"]) > max_jump_px:
                tr = {"id": f"trk-{next_id}", "last_frame": f, "last_uv": (u, v)}
                tracks[name] = tr
                colour_of[tr["id"]] = name
                next_id += 1
            tr["last_frame"], tr["last_uv"] = f, (u, v)
            obs.append(Observation(f, round(f / fps, 6), tr["id"], round(u, 3), round(v, 3), round(min(1.0, area / MARKER_AREA_PX), 4), None))
        f += 1
    cap.release()
    obs.sort(key=lambda o: (o.frame, o.track_id))
    return ObservationFile(Video(width, height, float(fps), Path(video_path).name), "synthetic marker tracking v1 (colour blobs; not person detection)", obs), colour_of
