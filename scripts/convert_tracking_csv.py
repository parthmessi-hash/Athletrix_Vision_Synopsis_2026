"""Convert a tracking (position/motion) CSV file into Athletrix's JSON Lines interchange format.

A documented, provider-agnostic CSV ADAPTER, not support for any vendor's export
and not a hardware or camera integration. It reads a CSV with a timestamp column
and the five measurement columns of the `TrackingSample` model, turns every row
into one `tracking` record of the normalized format replayed by
`scripts/replay_samples.py`, and writes a `.jsonl` file. It makes no network
requests.

WHAT THE CSV MUST LOOK LIKE (first version)
--------------------------------------------
- A header row naming the columns. The six columns are chosen explicitly
  (`--timestamp-column`, `--x-position-column`, `--y-position-column`,
  `--speed-mps-column`, `--acceleration-mps2-column`,
  `--tracking-confidence-column`; defaults are the model's field names). Each
  must appear exactly once in the header and the six names must differ. Other
  columns are ignored.
- Timestamps must be ISO 8601 with an explicit timezone, e.g.
  `2026-10-20T10:00:00Z` or `2026-10-20T12:00:00+02:00`. Timezone-naive
  values, numeric epochs, and invalid dates are rejected; nothing is guessed.
  The timestamp text (including its offset) is copied into the output as is.
- Measurements must ALREADY be in the model's units: positions in metres in
  the venue's coordinate frame, speed in metres per second (never negative),
  acceleration in metres per second squared (negative = decelerating), and
  tracking confidence on the model's 0.0-1.0 scale. No unit or scale
  conversion is done. Every measurement is required on every row: a blank
  cell is an error. Missing confidence is never filled in with 1, and speed or
  acceleration is never calculated from positions. Zeros are real values and
  are kept. Non-numeric or non-finite values, and values outside the model's
  ranges, are errors.
- `--athlete-id` and `--source` are required and apply to every row;
  `--source` must be one of the existing generic categories (`wearable`,
  `camera`, `manual`, `simulated`) and is never inferred from the file name or
  contents. `--device-id` is optional and, when given, is written to every
  row; without it the records carry no device id. A source is supplied
  metadata, not proof of where the data came from.

The whole file is validated first, using the same `TrackingSample` model as the
API; any problem is reported with its CSV row number (the header is row 1) and
column name, and then nothing is written. The output file must not already
exist, and it is written through a temporary file in the same folder, so a
failure never leaves a partial output behind. Rows keep their CSV order; the
replay script orders them by timestamp when sending.

USAGE
    python scripts/convert_tracking_csv.py scripts/examples/fictional_tracking.csv out.jsonl \
        --athlete-id athlete-1 --source simulated --device-id fictional-trk-csv-1 --dry-run
    python scripts/convert_tracking_csv.py export.csv out.jsonl --athlete-id athlete-2 --source camera \
        --timestamp-column time --x-position-column x --y-position-column y \
        --speed-mps-column speed --acceleration-mps2-column accel --tracking-confidence-column conf

Then replay `out.jsonl` with scripts/replay_samples.py. The recorded timestamps
stay historical: they will not appear in the dashboard's live window, and only
the newest 300 samples per athlete per stream are retained. Recorded clocks are
not synchronized or corrected: a tracking CSV and a heart-rate CSV line up in a
review only as far as their recorded timestamps already do.

EXIT CODES: 0 converted (or a valid dry run); 1 invalid input, output file
already present, or unreadable file; 2 bad command-line arguments.
"""

import argparse
import csv
import math
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # allow `python scripts/convert_tracking_csv.py` from the repository root
    sys.path.insert(0, str(ROOT))

from app.models import TrackingSample  # noqa: E402
from scripts.convert_heart_rate_csv import INTEGER, SOURCES, RowError, parse_timestamp, read_csv, write_jsonl  # noqa: E402

# Model measurement fields, in output order. The CSV column for each is chosen on the command line.
MEASUREMENTS = ("x_position", "y_position", "speed_mps", "acceleration_mps2", "tracking_confidence")
FIELDS = ("timestamp", *MEASUREMENTS)
UNITS = {
    "x_position": "metres",
    "y_position": "metres",
    "speed_mps": "metres per second",
    "acceleration_mps2": "metres per second squared",
    "tracking_confidence": "0.0-1.0 scale",
}


@dataclass(frozen=True)
class Converted:
    row: int
    record: dict            # the JSON Lines record to write
    timestamp: datetime     # parsed, for the description only


# ---- parsing ----------------------------------------------------------------------------------------------

def parse_measurement(text: str, field: str) -> tuple[float | int | None, str | None]:
    """(value, None) for a finite number; (None, reason) otherwise. Blank is an error: nothing is filled in."""
    t = text.strip()
    if not t:
        return None, f"value is missing; {field} is required and is never filled in or calculated"
    if INTEGER.match(t):
        return int(t), None
    try:
        value = float(t)
    except ValueError:
        return None, f"{t!r} is not a number ({field} must already be in {UNITS[field]})"
    if not math.isfinite(value):
        return None, f"{t!r} is not a finite number"
    return value, None


def read_header(header: list[str], columns: dict[str, str]) -> tuple[dict[str, int], list[RowError]]:
    """Field -> column index for the six required columns; errors for missing, duplicated or shared column names."""
    names = [h.strip() for h in header]
    if not any(names):
        return {}, [RowError(1, "row", "header row is empty")]
    shared = [name for name, n in Counter(columns.values()).items() if n > 1]
    if shared:
        fields = ", ".join(f for f in FIELDS if columns[f] in shared)
        return {}, [RowError(1, "row", f"columns {sorted(shared)} are named for more than one field ({fields}); the six column names must differ")]
    counts = Counter(names)
    index: dict[str, int] = {}
    errors: list[RowError] = []
    for field in FIELDS:
        column = columns[field]
        n = counts.get(column, 0)
        if n == 0:
            errors.append(RowError(1, column, f"column not found in header {names}" + (f" (needed for {field})" if column != field else "")))
        elif n > 1:
            errors.append(RowError(1, column, f"column appears {n} times in the header; the mapping is ambiguous"))
        else:
            index[field] = names.index(column)
    return index, errors


def convert_rows(
    rows: list[list[str]], *, columns: dict[str, str], athlete_id: str, source: str, device_id: str | None = None
) -> tuple[list[Converted], list[RowError]]:
    """Validate every CSV row (header first) and build the records. Errors never stop the scan, so all are reported."""
    if not rows:
        return [], [RowError(1, "row", "file is empty (no header row)")]
    index, errors = read_header(rows[0], columns)
    if errors:
        return [], errors
    width = len(rows[0])
    out: list[Converted] = []
    for offset, row in enumerate(rows[1:], start=2):
        if not any(cell.strip() for cell in row):
            continue  # blank line: ignored
        if len(row) != width:
            errors.append(RowError(offset, "row", f"has {len(row)} fields, header has {width}"))
            continue
        ts_text = row[index["timestamp"]].strip()
        ts, ts_err = parse_timestamp(ts_text)
        row_errors = [RowError(offset, columns["timestamp"], ts_err)] if ts_err else []
        sample: dict = {"athlete_id": athlete_id, "timestamp": ts_text}
        for field in MEASUREMENTS:
            value, err = parse_measurement(row[index[field]], field)
            if err:
                row_errors.append(RowError(offset, columns[field], err))
            else:
                sample[field] = value
        if row_errors:
            errors.extend(row_errors)
            continue
        sample["source"] = source
        if device_id is not None:
            sample["device_id"] = device_id
        try:
            TrackingSample.model_validate(sample)
        except ValidationError as exc:
            for err in exc.errors():
                loc = ".".join(str(p) for p in err["loc"])
                errors.append(RowError(offset, columns.get(loc, loc), err["msg"]))
            continue
        out.append(Converted(row=offset, record={"type": "tracking", "sample": sample}, timestamp=ts))
    if not out and not errors:
        errors.append(RowError(1, "row", "file has a header but no data rows"))
    return out, errors


# ---- description ----------------------------------------------------------------------------------------------

def describe(converted: list[Converted], src: Path, dst: Path, athlete_id: str, source: str, device_id: str | None) -> list[str]:
    stamps = [c.timestamp for c in converted]
    out_of_order = sum(1 for a, b in zip(stamps, stamps[1:]) if b < a)
    first, last = min(stamps), max(stamps)
    samples = [c.record["sample"] for c in converted]
    speeds = [s["speed_mps"] for s in samples]
    confidences = [s["tracking_confidence"] for s in samples]
    stationary = sum(1 for s in samples if s["speed_mps"] == 0)
    decelerating = sum(1 for s in samples if s["acceleration_mps2"] < 0)
    return [
        "TRACKING CSV CONVERSION",
        "Positions (m), speed (m/s), acceleration (m/s^2) and confidence (0-1) are taken as written; timestamps and offsets are copied as written. No hardware, no network.",
        f"Input: {src} -> {len(converted)} tracking records",
        f"Output: {dst}",
        f"athlete_id={athlete_id} source={source} device_id={device_id if device_id is not None else '(none given)'} (applied to every row; nothing inferred from the file)",
        f"Recorded range: {first.isoformat()} to {last.isoformat()} (span {(last - first).total_seconds():.3f} s)",
        f"speed_mps as written: {min(speeds)} to {max(speeds)}; {stationary} row(s) with speed 0; {decelerating} row(s) with negative acceleration",
        f"tracking_confidence as written: {min(confidences)} to {max(confidences)} (the engine applies its own minimum-confidence rule when reviewing)",
        (f"Rows are kept in CSV order; {out_of_order} row(s) are earlier than the row before them. The replay script orders records by timestamp."
         if out_of_order else "Rows are in chronological order and are kept in CSV order."),
    ]


# ---- command line -------------------------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", type=Path, help="CSV file with a header row")
    parser.add_argument("output", type=Path, help="JSON Lines file to create (must not exist)")
    parser.add_argument("--athlete-id", required=True, help="athlete_id for every record")
    parser.add_argument("--source", required=True, choices=SOURCES, help="generic source category for every record (never inferred)")
    parser.add_argument("--device-id", default=None, help="optional device_id for every record (omitted from the records when not given)")
    parser.add_argument("--timestamp-column", default="timestamp", help="header name of the ISO 8601 timestamp column (default 'timestamp')")
    for field in MEASUREMENTS:
        parser.add_argument(
            f"--{field.replace('_', '-')}-column", dest=f"{field}_column", default=field,
            help=f"header name of the {field} column, {UNITS[field]} (default {field!r})",
        )
    parser.add_argument("--dry-run", action="store_true", help="validate and describe the conversion; write nothing")
    return parser


def main(argv: list[str] | None = None, *, out: Callable[[str], None] = print) -> int:
    args = build_parser().parse_args(argv)
    src: Path = args.input
    dst: Path = args.output
    columns = {field: getattr(args, f"{field}_column") for field in FIELDS}
    if not src.is_file():
        out(f"Cannot read {src}: not a file")
        return 1
    if dst.exists():
        out(f"Refusing to write {dst}: it already exists (existing files are never overwritten; choose another name or remove it yourself)")
        return 1
    try:
        rows = read_csv(src)
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        out(f"Cannot read {src}: {exc}")
        return 1
    converted, errors = convert_rows(rows, columns=columns, athlete_id=args.athlete_id, source=args.source, device_id=args.device_id)
    if errors:
        out(f"TRACKING CSV CONVERSION: {src} is invalid; nothing was written.")
        for e in errors:
            out(f"  {e}")
        out(f"{len(errors)} error(s) on {len({e.row for e in errors})} row(s); {len(converted)} row(s) were valid.")
        return 1
    for line in describe(converted, src, dst, args.athlete_id, args.source, args.device_id):
        out(line)
    if args.dry_run:
        out("Dry run: the CSV is valid; no file was written.")
        return 0
    try:
        write_jsonl(converted, dst)
    except FileExistsError:
        out(f"Refusing to write {dst}: it appeared while converting; nothing was overwritten")
        return 1
    except OSError as exc:
        out(f"Cannot write {dst}: {exc}")
        return 1
    out(f"Wrote {len(converted)} record(s) to {dst}. Replay with: python scripts/replay_samples.py {dst} --dry-run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
