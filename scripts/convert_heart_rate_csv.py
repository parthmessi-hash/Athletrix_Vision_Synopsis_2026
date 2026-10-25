"""Convert a heart-rate CSV file into Athletrix's JSON Lines interchange format.

A documented, provider-agnostic CSV ADAPTER, not support for any vendor's export.
It reads a CSV with a timestamp column and a heart-rate column, turns every row
into one `physiological` record of the normalized format replayed by
`scripts/replay_samples.py`, and writes a `.jsonl` file. It makes no network
requests and does not connect to any hardware.

WHAT THE CSV MUST LOOK LIKE (first version)
--------------------------------------------
- A header row naming the columns. The timestamp and heart-rate columns are
  chosen explicitly (`--timestamp-column`, `--heart-rate-column`); each must
  appear exactly once in the header. Other columns are ignored.
- Timestamps must be ISO 8601 with an explicit timezone, e.g.
  `2026-10-20T10:00:00Z` or `2026-10-20T12:00:00+02:00`. Timezone-naive
  values, numeric epochs, and invalid dates are rejected; nothing is guessed.
  The timestamp text (including its offset) is copied into the output as is.
- Heart rate must ALREADY be in beats per minute (bpm). No unit conversion is
  done. A blank cell becomes `null` (heart rate not reported), never 0. Any
  non-numeric or non-finite value, or a value outside the model's range, is an
  error.
- `--athlete-id`, `--device-id` and `--source` are required and apply to every
  row. `--source` must be one of the existing generic source categories
  (`wearable`, `camera`, `manual`, `simulated`); it is never inferred from the
  file name or contents. No other measurement is invented.

The whole file is validated first, using the same `PhysiologicalSample` model
as the API; any problem is reported with its CSV row number (the header is row
1) and field name, and then nothing is written. The output file must not
already exist, and it is written through a temporary file in the same folder,
so a failure never leaves a partial output behind. Rows keep their CSV order;
the replay script orders them by timestamp when sending.

USAGE
    python scripts/convert_heart_rate_csv.py scripts/examples/fictional_heart_rate.csv out.jsonl \
        --athlete-id athlete-1 --device-id fictional-hr-csv-1 --source simulated --dry-run
    python scripts/convert_heart_rate_csv.py export.csv out.jsonl \
        --timestamp-column time --heart-rate-column hr --athlete-id athlete-2 --device-id strap-7 --source wearable

Then replay `out.jsonl` with scripts/replay_samples.py. The recorded timestamps
stay historical: they will not appear in the dashboard's live window, and only
the newest 300 samples per athlete per stream are retained.

EXIT CODES: 0 converted (or a valid dry run); 1 invalid input, output file
already present, or unreadable file; 2 bad command-line arguments.
"""

import argparse
import csv
import json
import math
import os
import re
import sys
import tempfile
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # allow `python scripts/convert_heart_rate_csv.py` from the repository root
    sys.path.insert(0, str(ROOT))

from app.models import PhysiologicalSample, SampleSource  # noqa: E402

DEFAULT_TIMESTAMP_COLUMN = "timestamp"
DEFAULT_HEART_RATE_COLUMN = "heart_rate_bpm"
SOURCES = [s.value for s in SampleSource]

# ISO 8601 date-time with a 'T' separator and an explicit UTC designator or numeric offset. Anything else is rejected.
ISO_WITH_TZ = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?(Z|[+-]\d{2}:\d{2})$")
INTEGER = re.compile(r"^[+-]?\d+$")


@dataclass(frozen=True)
class RowError:
    row: int        # CSV row number; the header is row 1
    field: str      # column name, or "row" for structural problems
    message: str

    def __str__(self) -> str:
        return f"row {self.row}, {self.field}: {self.message}"


@dataclass(frozen=True)
class Converted:
    row: int
    record: dict            # the JSON Lines record to write
    timestamp: datetime     # parsed, for the description only
    heart_rate: float | None


# ---- parsing ----------------------------------------------------------------------------------------------

def parse_timestamp(text: str) -> tuple[datetime | None, str | None]:
    """(aware datetime, None) for an ISO 8601 timestamp with an explicit timezone; (None, reason) otherwise."""
    t = text.strip()
    if not t:
        return None, "timestamp is empty"
    if INTEGER.match(t) or re.match(r"^[+-]?\d+\.\d+$", t):
        return None, "numeric epoch values are not accepted; use ISO 8601 with a timezone, e.g. 2026-10-20T10:00:00Z"
    if not ISO_WITH_TZ.match(t):
        return None, "must be ISO 8601 with an explicit timezone, e.g. 2026-10-20T10:00:00Z or 2026-10-20T12:00:00+02:00"
    try:
        parsed = datetime.fromisoformat(t.replace("Z", "+00:00"))
    except ValueError as exc:
        return None, f"not a valid date-time ({exc})"
    return parsed, None


def parse_heart_rate(text: str) -> tuple[float | int | None, str | None, bool]:
    """(value, reason, is_blank). A blank cell is null (not reported); the value keeps integer-ness for the output."""
    t = text.strip()
    if not t:
        return None, None, True
    if INTEGER.match(t):
        return int(t), None, False
    try:
        value = float(t)
    except ValueError:
        return None, f"{t!r} is not a number (heart rate must already be in bpm)", False
    if not math.isfinite(value):
        return None, f"{t!r} is not a finite number", False
    return value, None, False


def read_header(header: list[str], timestamp_column: str, heart_rate_column: str) -> tuple[dict[str, int], list[RowError]]:
    """Column name -> index for the two required columns; errors for missing, duplicated or identical names."""
    errors: list[RowError] = []
    names = [h.strip() for h in header]
    if not any(names):
        return {}, [RowError(1, "row", "header row is empty")]
    if timestamp_column == heart_rate_column:
        return {}, [RowError(1, "row", f"timestamp and heart-rate columns are both {timestamp_column!r}; they must differ")]
    counts = Counter(names)
    index: dict[str, int] = {}
    for column in (timestamp_column, heart_rate_column):
        n = counts.get(column, 0)
        if n == 0:
            errors.append(RowError(1, column, f"column not found in header {names}"))
        elif n > 1:
            errors.append(RowError(1, column, f"column appears {n} times in the header; the mapping is ambiguous"))
        else:
            index[column] = names.index(column)
    return index, errors


def convert_rows(
    rows: list[list[str]],
    *,
    timestamp_column: str,
    heart_rate_column: str,
    athlete_id: str,
    device_id: str,
    source: str,
) -> tuple[list[Converted], list[RowError]]:
    """Validate every CSV row (header first) and build the records. Errors never stop the scan, so all are reported."""
    if not rows:
        return [], [RowError(1, "row", "file is empty (no header row)")]
    index, errors = read_header(rows[0], timestamp_column, heart_rate_column)
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
        ts_text = row[index[timestamp_column]].strip()
        ts, ts_err = parse_timestamp(ts_text)
        hr, hr_err, _blank = parse_heart_rate(row[index[heart_rate_column]])
        if ts_err:
            errors.append(RowError(offset, timestamp_column, ts_err))
        if hr_err:
            errors.append(RowError(offset, heart_rate_column, hr_err))
        if ts_err or hr_err:
            continue
        sample = {"athlete_id": athlete_id, "timestamp": ts_text, "source": source, "device_id": device_id, "heart_rate_bpm": hr}
        try:
            PhysiologicalSample.model_validate(sample)
        except ValidationError as exc:
            for err in exc.errors():
                loc = ".".join(str(p) for p in err["loc"])
                errors.append(RowError(offset, heart_rate_column if loc == "heart_rate_bpm" else loc, err["msg"]))
            continue
        out.append(Converted(row=offset, record={"type": "physiological", "sample": sample}, timestamp=ts, heart_rate=hr))
    if not out and not errors:
        errors.append(RowError(1, "row", "file has a header but no data rows"))
    return out, errors


def read_csv(path: Path) -> list[list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:  # utf-8-sig drops a BOM some spreadsheet tools write
        return [row for row in csv.reader(fh)]


# ---- description and output ----------------------------------------------------------------------------------

def describe(converted: list[Converted], src: Path, dst: Path, athlete_id: str, device_id: str, source: str) -> list[str]:
    stamps = [c.timestamp for c in converted]
    nulls = sum(1 for c in converted if c.heart_rate is None)
    out_of_order = sum(1 for a, b in zip(stamps, stamps[1:]) if b < a)
    first, last = min(stamps), max(stamps)
    return [
        "HEART-RATE CSV CONVERSION",
        "Heart rate is taken as bpm as written; timestamps and offsets are copied as written; blank heart rates become null. No hardware, no network.",
        f"Input: {src} -> {len(converted)} physiological records ({nulls} with heart rate null)",
        f"Output: {dst}",
        f"athlete_id={athlete_id} device_id={device_id} source={source} (applied to every row; nothing inferred from the file)",
        f"Recorded range: {first.isoformat()} to {last.isoformat()} (span {(last - first).total_seconds():.3f} s)",
        (f"Rows are kept in CSV order; {out_of_order} row(s) are earlier than the row before them. The replay script orders records by timestamp."
         if out_of_order else "Rows are in chronological order and are kept in CSV order."),
    ]


def write_jsonl(converted: list[Converted], dst: Path) -> None:
    """Write through a temporary file in the destination folder and move it into place; never overwrite."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dst.name}.", suffix=".part", dir=dst.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for c in converted:
                fh.write(json.dumps(c.record, ensure_ascii=False) + "\n")
        if dst.exists():  # re-checked at the last moment
            raise FileExistsError(dst)
        os.replace(tmp, dst)
    finally:
        if tmp.exists():
            tmp.unlink()


# ---- command line -------------------------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", type=Path, help="CSV file with a header row")
    parser.add_argument("output", type=Path, help="JSON Lines file to create (must not exist)")
    parser.add_argument("--timestamp-column", default=DEFAULT_TIMESTAMP_COLUMN, help=f"header name of the timestamp column (default {DEFAULT_TIMESTAMP_COLUMN!r})")
    parser.add_argument("--heart-rate-column", default=DEFAULT_HEART_RATE_COLUMN, help=f"header name of the heart-rate (bpm) column (default {DEFAULT_HEART_RATE_COLUMN!r})")
    parser.add_argument("--athlete-id", required=True, help="athlete_id for every record")
    parser.add_argument("--device-id", required=True, help="device_id for every record")
    parser.add_argument("--source", required=True, choices=SOURCES, help="generic source category for every record (never inferred)")
    parser.add_argument("--dry-run", action="store_true", help="validate and describe the conversion; write nothing")
    return parser


def main(argv: list[str] | None = None, *, out: Callable[[str], None] = print) -> int:
    args = build_parser().parse_args(argv)
    src: Path = args.input
    dst: Path = args.output
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
    converted, errors = convert_rows(
        rows, timestamp_column=args.timestamp_column, heart_rate_column=args.heart_rate_column,
        athlete_id=args.athlete_id, device_id=args.device_id, source=args.source,
    )
    if errors:
        out(f"HEART-RATE CSV CONVERSION: {src} is invalid; nothing was written.")
        for e in errors:
            out(f"  {e}")
        out(f"{len(errors)} error(s) on {len({e.row for e in errors})} row(s); {len(converted)} row(s) were valid.")
        return 1
    for line in describe(converted, src, dst, args.athlete_id, args.device_id, args.source):
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
