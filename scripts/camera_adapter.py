"""Turn camera observations into TrackingSample records: validate, export JSON Lines, or send to a local engine.

USAGE
    python scripts/camera_adapter.py validate OBS.jsonl --calibration CAL.json --config CONFIG.json [--assignments A.json]
    python scripts/camera_adapter.py export   OBS.jsonl --calibration CAL.json --config CONFIG.json [--assignments A.json] \\
                                              --anchor-utc 2026-10-29T12:00:00Z --out samples.jsonl --log adapter-log.json
    python scripts/camera_adapter.py send     ... same inputs ... --anchor-utc now|ISO --url http://127.0.0.1:8000 [--speed 1] \\
                                              [--out samples.jsonl] [--log adapter-log.json]

  validate  checks every input and runs the whole pipeline; writes and sends nothing.
  export    writes the samples in the engine's interchange format ({"type": "tracking", "sample": {...}}) plus the
            adapter log (skips with reasons, resets, unassigned tracks, time mapping, config and calibration hashes,
            and the video time and frame behind every sample).
  send      exports in memory, then posts through scripts/replay_samples.py (same validation, pacing on a monotonic
            clock, stop at the first failure, every record counted as submitted, failed or not sent). With
            --anchor-utc now the anchor is the current UTC time when sending starts and --speed 1 paces samples as
            they occur in the video (the first sample is timestamped when sending starts): a live-paced PLAYBACK of a
            file, not live camera capture.

TIME: sample timestamp = anchor + (video_time_s - 0). The anchor is never guessed: --anchor-utc is required, its meaning
is recorded in the log, and timestamps are never rewritten afterwards to get past the engine's checks (a sample more
than 5 s ahead of engine time is rejected by the engine, and the send stops there).

SOURCE: config.source must be "simulated" for synthetic markers; "camera" is for a real camera's measurements.

EXIT CODES: 0 ok · 1 invalid input or refused · 2 bad arguments · 3 the send stopped on a failed request
"""

import argparse
import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import replay_samples  # noqa: E402
from camera_adapter import calibration as cal  # noqa: E402
from camera_adapter import observations as obsmod  # noqa: E402
from camera_adapter.pipeline import AdapterConfig, TimeMapping, adapter_log, process, records, write_jsonl  # noqa: E402


def parse_anchor(text: str, now: Callable[[], datetime], first_output_s: float) -> TimeMapping:
    if text == "now":   # the first produced sample is timestamped at the moment sending starts, later ones by their video time
        return TimeMapping(now() - timedelta(seconds=first_output_s), 0.0,
                           f"live-paced playback: anchor = UTC time when sending started minus the first sample's video time ({first_output_s:g} s); not camera capture time")
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("--anchor-utc needs a timezone (for example 2026-10-29T12:00:00Z)")
    return TimeMapping(dt.astimezone(UTC), 0.0, "anchor stated with --anchor-utc; not camera capture time")


def main(argv: list[str] | None = None, *, out: Callable[[str], None] = print, now: Callable[[], datetime] = lambda: datetime.now(UTC),
         make_client: Callable[[str], httpx.Client] = lambda url: httpx.Client(base_url=url, timeout=10.0)) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["validate", "export", "send"])
    p.add_argument("observations", type=Path)
    p.add_argument("--calibration", type=Path, required=True)
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--assignments", type=Path)
    p.add_argument("--anchor-utc")
    p.add_argument("--out", type=Path)
    p.add_argument("--log", type=Path)
    p.add_argument("--url", default="http://127.0.0.1:8000")
    p.add_argument("--speed", type=replay_samples.validate_speed, default=1.0)
    try:
        args = p.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    if args.command != "validate" and not args.anchor_utc:
        out("REFUSED: --anchor-utc is required to map video-relative time to UTC (an ISO time with timezone, or 'now' for live-paced playback)")
        return 2
    if args.command == "export" and not args.out:
        out("REFUSED: export needs --out")
        return 2
    for path in (args.out, args.log):
        if path is not None and path.exists():
            out(f"REFUSED: {path} already exists; nothing was written")
            return 1
    try:
        obs = obsmod.load(args.observations)
        if args.assignments:
            obs = obsmod.apply_assignments(obs, obsmod.load_assignments(args.assignments))
        calib = cal.load(args.calibration)
        config = AdapterConfig.from_doc(json.loads(args.config.read_text(encoding="utf-8")))
        res = process(obs, calib, config)
        mapping = parse_anchor(args.anchor_utc, now, min((o.video_time_s for o in res.outputs), default=0.0)) if args.anchor_utc else None
        recs = records(res, mapping or TimeMapping(datetime(2000, 1, 1, tzinfo=UTC), 0.0, "validation only"), config)
    except (obsmod.ObservationError, cal.CalibrationError, ValueError, OSError, json.JSONDecodeError, TypeError) as exc:
        out("INVALID INPUT: nothing was produced or sent.")
        for e in getattr(exc, "errors", [str(exc)]):
            out(f"  - {e}")
        return 1
    log = adapter_log(res, obs=obs, calibration_path=args.calibration, calibration=calib, config=config, mapping=mapping, observations_path=args.observations)
    out(f"{len(obs.observations)} observations -> {len(recs)} tracking samples for {len(res.per_athlete)} athletes; skipped: " +
        (", ".join(f"{k} {v}" for k, v in sorted(res.skipped.items())) or "none") + f"; estimator resets: {len(res.resets)}; unassigned tracks: {', '.join(sorted(res.unassigned)) or 'none'}")
    for a, s in sorted(res.per_athlete.items()):
        out(f"  {a}: {s['observations']} observations, {s['outputs']} samples, {s.get('warm_up', 0)} warm-up skips")
    if mapping:
        out(f"  time mapping: {mapping.describe()}")
    if args.command == "validate":
        out("VALID: nothing was written or sent.")
        return 0
    if args.out:
        write_jsonl(recs, args.out)
        out(f"wrote {args.out}")
    if args.log:
        args.log.write_text(json.dumps(log, indent=2) + "\n", encoding="utf-8")
        out(f"wrote {args.log}")
    if args.command == "export":
        return 0
    loaded = [replay_samples.parse_line(i + 1, json.dumps(r)) for i, r in enumerate(recs)]
    bad = [x for x in loaded if isinstance(x, list)]
    if bad:
        out(f"INVALID SAMPLES: {bad[0]}")
        return 1
    ordered = replay_samples.order_records(loaded)
    with make_client(args.url) as client:
        result = replay_samples.replay(ordered, client, args.speed, out=lambda _: None)
    for line in replay_samples.summarize(result, len(ordered)):
        out(line)
    return 0 if result.failed == 0 else 3


if __name__ == "__main__":
    sys.exit(main())
