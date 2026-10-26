"""Replay recorded samples from an Athletrix JSON Lines file into a running backend.

RECORDED DATA REPLAY. This script sends previously recorded samples exactly as
they were recorded: timestamps, sources, identifiers, null measurements, zero
values and tracking confidences are preserved. Nothing is invented, nothing is
shifted to the current time, and every record goes through the backend's normal
ingestion endpoint and validation (including the future-timestamp check).

FILE FORMAT: the Athletrix normalized interchange format
-----------------------------------------------------------
JSON Lines (.jsonl): one JSON object per line, UTF-8, with exactly two keys:

    {"type": "physiological", "sample": { ...PhysiologicalSample fields... }}
    {"type": "tracking",      "sample": { ...TrackingSample fields... }}

`sample` must validate against the existing Pydantic models in `app/models/`
(same required fields, ranges and "unknown fields rejected" rule as the API;
timestamps must be ISO 8601 with a timezone). Blank lines are ignored. Records
may appear in any order; the replay sorts them by recorded timestamp and keeps
the input order for equal timestamps.

This is Athletrix's own normalized format, produced by hand or by an export
step. The script does NOT parse arbitrary vendor CSV files and does NOT connect
to wearable or camera hardware; mapping a vendor export onto this format is a
separate step.

PLAYBACK
--------
The whole file is validated before any request is sent; an invalid file is
reported line by line with field-level errors and nothing is posted. Valid
records are posted one at a time, in chronological order, each to its own
existing endpoint (`/api/v1/samples/physiological` or `/api/v1/samples/tracking`).
Requests are paced by the recorded timestamp differences divided by `--speed`
(a positive, finite multiplier; 2 means twice as fast, 0.5 half speed) using a
monotonic clock; changing the speed never changes the sample timestamps that
are sent. On the first HTTP error or connection failure the replay stops and
reports exactly what was submitted, what failed and what was not sent. Nothing
is retried or skipped silently.

Because timestamps are preserved, replayed recordings stay historical: the
backend's newest-timestamp rules, per-stream retention caps per athlete (default 300),
and the frontend's rolling 60-second window all apply to the recorded times.

USAGE (backend must already be running)
    python scripts/replay_samples.py scripts/examples/fictional_replay.jsonl --dry-run
    python scripts/replay_samples.py scripts/examples/fictional_replay.jsonl
    python scripts/replay_samples.py recording.jsonl --url http://127.0.0.1:8010 --speed 4

EXIT CODES: 0 all records submitted (or a valid dry run); 1 the file is invalid
or unreadable; 2 the replay stopped on a request failure.
"""

import argparse
import json
import math
import sys
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # allow `python scripts/replay_samples.py` from the repository root
    sys.path.insert(0, str(ROOT))

from app.core.clock import MAX_FUTURE_TIMESTAMP_SKEW  # noqa: E402
from app.models import PhysiologicalSample, TrackingSample  # noqa: E402

MODELS = {"physiological": PhysiologicalSample, "tracking": TrackingSample}
ENDPOINTS = {"physiological": "/api/v1/samples/physiological", "tracking": "/api/v1/samples/tracking"}
RECORD_KEYS = {"type", "sample"}
DEFAULT_URL = "http://127.0.0.1:8000"


@dataclass(frozen=True)
class Record:
    """One validated line. `payload` is the sample object exactly as written in the file; that is what is sent."""

    line: int
    type: str
    payload: dict
    athlete_id: str
    timestamp: datetime  # timezone-aware, from the validated model

    @property
    def endpoint(self) -> str:
        return ENDPOINTS[self.type]


@dataclass(frozen=True)
class FileError:
    line: int
    message: str

    def __str__(self) -> str:
        return f"line {self.line}: {self.message}"


@dataclass
class ReplayResult:
    submitted: int = 0
    failed: int = 0
    not_sent: int = 0
    failure: str | None = None  # description of the request that stopped the replay

    @property
    def ok(self) -> bool:
        return self.failed == 0 and self.not_sent == 0


# ---- parsing and validation -------------------------------------------------------------------------

def _format_validation_error(exc: ValidationError) -> list[str]:
    out = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "(record)"
        out.append(f"sample.{loc}: {err['msg']}")
    return out


def parse_line(line_no: int, text: str) -> Record | list[FileError]:
    """Validate one non-blank line. Returns a Record, or the list of errors found on that line."""
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        return [FileError(line_no, f"not valid JSON ({exc.msg} at column {exc.colno})")]
    if not isinstance(obj, dict):
        return [FileError(line_no, "record must be a JSON object with keys 'type' and 'sample'")]
    keys = set(obj)
    if keys != RECORD_KEYS:
        missing, extra = sorted(RECORD_KEYS - keys), sorted(keys - RECORD_KEYS)
        parts = ([f"missing key(s): {', '.join(missing)}"] if missing else []) + ([f"unexpected key(s): {', '.join(extra)}"] if extra else [])
        return [FileError(line_no, "; ".join(parts))]
    rtype = obj["type"]
    if rtype not in MODELS:
        return [FileError(line_no, f"unknown record type {rtype!r}; expected one of: {', '.join(MODELS)}")]
    if not isinstance(obj["sample"], dict):
        return [FileError(line_no, "'sample' must be a JSON object")]
    try:
        model = MODELS[rtype].model_validate(obj["sample"])
    except ValidationError as exc:
        return [FileError(line_no, msg) for msg in _format_validation_error(exc)]
    return Record(line=line_no, type=rtype, payload=obj["sample"], athlete_id=model.athlete_id, timestamp=model.timestamp)


def load_file(path: Path) -> tuple[list[Record], list[FileError]]:
    """Validate every line of the file. Records are returned in file order; errors carry line numbers."""
    records: list[Record] = []
    errors: list[FileError] = []
    with path.open("r", encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, start=1):
            text = raw.strip()
            if not text:
                continue  # blank lines are ignored
            parsed = parse_line(line_no, text)
            if isinstance(parsed, Record):
                records.append(parsed)
            else:
                errors.extend(parsed)
    if not records and not errors:
        errors.append(FileError(0, "file contains no records"))
    return records, errors


def order_records(records: list[Record]) -> list[Record]:
    """Chronological order by recorded timestamp (compared as instants); input order is kept for equal timestamps."""
    return sorted(records, key=lambda r: r.timestamp)  # sorted() is stable


# ---- description -------------------------------------------------------------------------------------

def validate_speed(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid speed {text!r}: must be a number") from None
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError(f"invalid speed {text!r}: must be a positive finite number")
    return value


def recorded_span_seconds(ordered: list[Record]) -> float:
    return (ordered[-1].timestamp - ordered[0].timestamp).total_seconds() if ordered else 0.0


def playback_duration_seconds(ordered: list[Record], speed: float) -> float:
    return recorded_span_seconds(ordered) / speed


def describe(ordered: list[Record], path: Path, speed: float, now: datetime | None = None) -> list[str]:
    """Human-readable description of a validated, ordered record list (printed for dry runs and before a replay)."""
    by_type = Counter(r.type for r in ordered)
    by_athlete: dict[str, Counter] = {}
    for r in ordered:
        by_athlete.setdefault(r.athlete_id, Counter())[r.type] += 1
    lines = [
        "RECORDED DATA REPLAY",
        "Timestamps, sources, identifiers, nulls, zeros and confidences are sent exactly as recorded; playback speed only changes the pacing.",
        f"File: {path} ({len(ordered)} records: {by_type.get('physiological', 0)} physiological, {by_type.get('tracking', 0)} tracking)",
        "Athletes: " + ", ".join(f"{a} ({c.get('physiological', 0)} physiological, {c.get('tracking', 0)} tracking)" for a, c in sorted(by_athlete.items())),
        f"Recorded range: {ordered[0].timestamp.isoformat()} to {ordered[-1].timestamp.isoformat()} (span {recorded_span_seconds(ordered):.3f} s)",
        f"Playback: speed x{speed:g}, scheduled duration {playback_duration_seconds(ordered, speed):.3f} s",
    ]
    now = now or datetime.now(UTC)
    future = sum(1 for r in ordered if r.timestamp - now > MAX_FUTURE_TIMESTAMP_SKEW)
    if future:
        lines.append(f"Warning: {future} record(s) are more than {MAX_FUTURE_TIMESTAMP_SKEW.total_seconds():g} s ahead of this machine's clock; the backend will reject them (422).")
    age = (now - ordered[-1].timestamp).total_seconds()
    if age > 60:
        lines.append(f"Note: the newest record is {age:.0f} s old. Replayed samples keep their recorded times, so they will not appear in the dashboard's rolling 60-second window and will not replace a newer stored latest sample.")
    return lines


# ---- replay ---------------------------------------------------------------------------------------------

def replay(
    ordered: list[Record],
    client: httpx.Client,
    speed: float,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    out: Callable[[str], None] = print,
) -> ReplayResult:
    """Post the records in order, paced by recorded time differences / speed on a monotonic clock.

    Stops at the first failed request. The result counts every record exactly once as submitted,
    failed or not sent.
    """
    result = ReplayResult()
    if not ordered:
        return result
    start = clock()
    t0 = ordered[0].timestamp
    for i, r in enumerate(ordered):
        due = start + (r.timestamp - t0).total_seconds() / speed
        wait = due - clock()
        if wait > 0:
            sleep(wait)
        try:
            resp = client.post(r.endpoint, json=r.payload)
        except httpx.HTTPError as exc:
            result.failed = 1
            result.not_sent = len(ordered) - i - 1
            result.failure = f"line {r.line} ({r.type}, {r.athlete_id} @ {r.payload.get('timestamp')}): {type(exc).__name__}: {exc or 'no details'}"
            out(f"  line {r.line:<5} {r.type:<13} {r.athlete_id:<12} {r.payload.get('timestamp')}  FAILED {type(exc).__name__}")
            break
        if 200 <= resp.status_code < 300:
            result.submitted += 1
            out(f"  line {r.line:<5} {r.type:<13} {r.athlete_id:<12} {r.payload.get('timestamp')}  HTTP {resp.status_code}")
            continue
        result.failed = 1
        result.not_sent = len(ordered) - i - 1
        detail = resp.text[:300]
        result.failure = f"line {r.line} ({r.type}, {r.athlete_id} @ {r.payload.get('timestamp')}): HTTP {resp.status_code}: {detail}"
        out(f"  line {r.line:<5} {r.type:<13} {r.athlete_id:<12} {r.payload.get('timestamp')}  FAILED HTTP {resp.status_code}")
        break
    return result


def summarize(result: ReplayResult, total: int) -> list[str]:
    lines = [f"Result: {result.submitted} submitted, {result.failed} failed, {result.not_sent} not sent (of {total})."]
    if result.failure:
        lines.append(f"Stopped at {result.failure}")
        if "Timeout" in result.failure:
            lines.append("The backend's response to the failed request was not received; it may or may not have stored that sample.")
        lines.append("Records marked not sent were never posted. Fix the cause and rerun; equal-timestamp replays replace the stored sample for that instant, so rerunning is safe.")
    return lines


# ---- command line ---------------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("file", type=Path, help="JSON Lines file in the Athletrix interchange format")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"Backend base URL (default {DEFAULT_URL})")
    parser.add_argument("--speed", type=validate_speed, default=1.0, help="Playback multiplier: positive, finite (default 1 = recorded pace)")
    parser.add_argument("--dry-run", action="store_true", help="Validate and describe the file; send no requests")
    return parser


def main(
    argv: list[str] | None = None,
    *,
    make_client: Callable[[str], httpx.Client] = lambda url: httpx.Client(base_url=url, timeout=10.0),
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    out: Callable[[str], None] = print,
) -> int:
    args = build_parser().parse_args(argv)
    path: Path = args.file
    if not path.is_file():
        out(f"Cannot read {path}: not a file")
        return 1
    try:
        records, errors = load_file(path)
    except (OSError, UnicodeDecodeError) as exc:
        out(f"Cannot read {path}: {exc}")
        return 1
    if errors:
        out(f"RECORDED DATA REPLAY: {path} is invalid; nothing was sent.")
        for e in errors:
            out(f"  {e}")
        out(f"{len(errors)} error(s) on {len({e.line for e in errors})} line(s); {len(records)} record(s) were valid.")
        return 1

    ordered = order_records(records)
    for line in describe(ordered, path, args.speed):
        out(line)
    if args.dry_run:
        out("Dry run: the file is valid; no requests were sent.")
        return 0

    base = args.url.rstrip("/")
    out(f"Sending to {base} ...")
    with make_client(base) as client:
        result = replay(ordered, client, args.speed, clock=clock, sleep=sleep, out=out)
    for line in summarize(result, len(ordered)):
        out(line)
    return 0 if result.ok else 2


if __name__ == "__main__":
    sys.exit(main())
