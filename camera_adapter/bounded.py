"""Bounded frame processing for eventual live capture: frames that cannot be processed in time are dropped, never queued.

A live camera produces frames whether or not the detector has finished the previous one. Offline file analysis
(person_video.track_video) processes every frame and simply takes longer; that is not possible live. This module
puts a bounded queue between a frame source and the per-frame processor:

  source thread    takes each frame as the source releases it, with its index and capture time (seconds from the
                   source's first frame, on the source's clock). With `process_every_nth` > 1 it forwards only every
                   nth frame (deterministic sampling, counted as `sampled_out`). A forwarded frame enters a queue of at
                   most `queue_size` frames; when the queue is full the OLDEST waiting frame is dropped (counted, with
                   its index and capture time), so the processor always works on recent frames and no backlog grows.
  processor        takes the oldest waiting frame and runs the processing function on it with its original index and
                   capture time. Observation times are those capture times, never processing times, so a late
                   result still describes the instant the frame was taken.

The log separates: frames released by the source, sampled out, dropped (and the longest run of consecutive drops in
seconds), processed; queue wait, processing time and total processing lag (frame available -> its result ready) per
processed frame; whether processing kept up (nothing dropped and the lag p95 within one source frame period). This
processing lag is not the engine's receipt or display delay, which the engine's own timing instrumentation reports.

Continuity and identity after skipped work: the tracker is updated with capture times, so a dropped frame is a gap
in time. Its track-loss budget is time-based; the adapter's estimator resets after a gap longer than its max_gap_s and
starts a new continuity segment, so the engine never integrates across work that was dropped. Nothing is invented
for a dropped frame.

`paced_file_source` releases a video file's frames at the file's own rate on a monotonic clock: a PACED FILE REPLAY
that exercises the queue without a camera. It is not live-camera validation. No physical camera source is included:
none was available to test one.
"""

from __future__ import annotations

import math
import statistics
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class BoundedConfig:
    queue_size: int = 2
    process_every_nth: int = 1
    overflow: str = "drop_oldest"

    def validate(self) -> list[str]:
        e = []
        if not (isinstance(self.queue_size, int) and 1 <= self.queue_size <= 64):
            e.append("queue_size must be an integer from 1 to 64")
        if not (isinstance(self.process_every_nth, int) and 1 <= self.process_every_nth <= 30):
            e.append("process_every_nth must be an integer from 1 to 30")
        if self.overflow != "drop_oldest":
            e.append("overflow must be drop_oldest (the only policy)")
        return e


@dataclass
class Frame:
    index: int
    capture_s: float
    data: object
    available_at: float = 0.0     # monotonic time when the source released it


@dataclass
class BoundedResult:
    released: int = 0
    sampled_out: int = 0
    dropped: list[tuple[int, float]] = field(default_factory=list)   # (index, capture_s)
    processed: list[dict] = field(default_factory=list)              # {"index", "capture_s", "queue_wait_s", "processing_s", "lag_s"}
    wall_s: float = 0.0


def _stats(values: list[float]) -> dict | None:
    if not values:
        return None
    v = sorted(values)
    return {"median": round(statistics.median(v), 4), "p95": round(v[min(len(v) - 1, math.ceil(0.95 * len(v)) - 1)], 4), "max": round(v[-1], 4)}


def run_bounded(source: Iterable[tuple[int, float, object]], process: Callable[[int, float, object], object], config: BoundedConfig,
                *, clock: Callable[[], float] = time.monotonic) -> BoundedResult:
    """Feed `source` (index, capture_s, frame) through the bounded queue into `process(index, capture_s, frame)`."""
    errors = config.validate()
    if errors:
        raise ValueError("; ".join(errors))
    queue: deque[Frame] = deque()
    lock = threading.Condition()
    done = threading.Event()
    res = BoundedResult()
    started = clock()

    def produce() -> None:
        try:
            for index, capture_s, data in source:
                with lock:
                    res.released += 1
                    if index % config.process_every_nth:
                        res.sampled_out += 1
                        continue
                    if len(queue) >= config.queue_size:
                        old = queue.popleft()
                        res.dropped.append((old.index, old.capture_s))
                    queue.append(Frame(index, capture_s, data, clock()))
                    lock.notify()
        finally:
            done.set()
            with lock:
                lock.notify()

    producer = threading.Thread(target=produce, name="athletrix-frame-source", daemon=True)
    producer.start()
    while True:
        with lock:
            while not queue and not done.is_set():
                lock.wait(0.1)
            if not queue:
                if done.is_set():
                    break
                continue
            item = queue.popleft()
        begin = clock()
        process(item.index, item.capture_s, item.data)
        end = clock()
        res.processed.append({"index": item.index, "capture_s": round(item.capture_s, 6), "queue_wait_s": round(begin - item.available_at, 6),
                              "processing_s": round(end - begin, 6), "lag_s": round(end - item.available_at, 6)})
    producer.join()
    res.wall_s = clock() - started
    return res


def bounded_log(res: BoundedResult, config: BoundedConfig, source_fps: float, source_desc: str) -> dict:
    period = 1.0 / source_fps if source_fps > 0 else None
    lags = [p["lag_s"] for p in res.processed]
    runs, longest = [], 0.0
    drops = sorted(res.dropped)
    for i, (idx, cap) in enumerate(drops):     # longest run of consecutively dropped forwarded frames, in seconds of capture time
        if runs and idx == runs[-1][-1][0] + config.process_every_nth:
            runs[-1].append((idx, cap))
        else:
            runs.append([(idx, cap)])
    for r in runs:
        longest = max(longest, r[-1][1] - r[0][1] + (period or 0) * config.process_every_nth)
    lag_stats = _stats(lags)
    kept_up = not res.dropped and lag_stats is not None and period is not None and lag_stats["p95"] <= period * config.process_every_nth
    return {"mode": "bounded", "source": source_desc, "config": asdict(config), "source_fps": source_fps,
            "frames": {"released_by_source": res.released, "sampled_out": res.sampled_out, "dropped": len(res.dropped), "processed": len(res.processed),
                       "dropped_examples": [{"index": i, "capture_s": round(c, 6)} for i, c in drops[:20]], "longest_drop_run_s": round(longest, 3)},
            "processing_lag_s": lag_stats, "queue_wait_s": _stats([p["queue_wait_s"] for p in res.processed]),
            "processing_time_s": _stats([p["processing_s"] for p in res.processed]), "wall_s": round(res.wall_s, 3),
            "kept_up": kept_up,
            "notes": ["Observation times are the frames' capture times, not processing times.",
                      "processing_lag_s runs from a frame becoming available to its result being ready; it excludes the engine's receipt and display delay.",
                      "A dropped frame is a gap in time: nothing is invented for it, the tracker and estimator see the gap, and continuity breaks follow the usual rules."]}


def paced_file_source(path, *, speed: float = 1.0, max_frames: int | None = None,
                      clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> Iterator[tuple[int, float, object]]:
    """Release a video file's frames at its own frame rate (times `speed`), as a camera would: a paced file replay."""
    import cv2  # noqa: PLC0415
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise ValueError(f"cannot open video {path}")
    fps = float(cap.get(cv2.CAP_PROP_FPS))
    start = clock()
    f = 0
    try:
        while max_frames is None or f < max_frames:
            ok, frame = cap.read()
            if not ok:
                break
            due = start + (f / fps) / speed
            wait = due - clock()
            if wait > 0:
                sleep(wait)
            yield f, f / fps, frame
            f += 1
    finally:
        cap.release()


def synthetic_source(n: int, fps: float, make_frame: Callable[[int], object], *, clock: Callable[[], float] = time.monotonic,
                     sleep: Callable[[float], None] = time.sleep) -> Iterator[tuple[int, float, object]]:
    """Release n synthetic frames at `fps` in real time (tests and benchmarks without a camera)."""
    start = clock()
    for f in range(n):
        wait = start + f / fps - clock()
        if wait > 0:
            sleep(wait)
        yield f, f / fps, make_frame(f)
