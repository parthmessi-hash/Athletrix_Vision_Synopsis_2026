"""Inspect, validate, export and replay wearable data through the shared, vendor-independent foundation (wearable/).

USAGE
    python scripts/wearable_tool.py mapping  --provider P
    python scripts/wearable_tool.py inspect  FILE --provider P
    python scripts/wearable_tool.py validate FILE --provider P --assignments A.json [clock options]
    python scripts/wearable_tool.py export   FILE --provider P --assignments A.json [clock options] --out samples.jsonl \\
                                             [--provenance prov.jsonl] [--status-log status.jsonl] [--report report.json]
    python scripts/wearable_tool.py replay   FILE --provider P --assignments A.json [clock options] --url http://127.0.0.1:8000 [--speed 1]

PROVIDERS: fictionstrap-csv and imaginaryvest-jsonl are FICTIONAL formats that exercise the foundation. No real device
is supported yet; Zephyr's boundary is wearable/providers/zephyr.py, deliberately unimplemented until the vendor's
documentation and sample payloads are in hand (docs/zephyr-integration-checklist.md).

CLOCK OPTIONS (wearable/clock.py): --clock-basis device_utc|device_offset|receipt_minus (default device_utc),
  --offset-s S (device_offset: UTC = device time + S), --latency-s S (receipt_minus), --uncertainty-s S (omit: unknown).

  mapping   prints the provider's documented field/unit mapping, unmapped fields with the reason, and quality fields.
  inspect   reads the file only: records per device and kind, format problems per line.
  validate  runs the whole normalization; writes and sends nothing; prints the account (samples, skips with reasons,
            value issues, conflicts, ordering, device-status history).
  export    writes PhysiologicalSample records in the engine's interchange format ({"type": "physiological", ...}),
            optionally one provenance line per sample (raw values, mapping, clock basis and uncertainty, receipt time,
            assignment, unmapped fields present) and the device-status history (connection, battery, signal; the engine
            keeps only each device's latest status).
  replay    exports in memory and posts through scripts/replay_samples.py (validation, monotonic pacing, stop on the
            first failure). This is a paced replay of a file, not a live device connection.
Use fictional data or data you are permitted to process; outputs belong in an ignored folder, never in a commit.

EXIT CODES: 0 ok · 1 invalid input or refused · 2 bad arguments · 3 the replay stopped on a failed request
"""

import argparse
import json
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import replay_samples  # noqa: E402
from wearable import assignments as asg  # noqa: E402
from wearable.clock import ClockMapping  # noqa: E402
from wearable.normalize import normalize  # noqa: E402
from wearable.providers.fictional import ADAPTERS  # noqa: E402


def write_jsonl(rows, path: Path) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def main(argv: list[str] | None = None, *, out: Callable[[str], None] = print,
         make_client: Callable[[str], httpx.Client] = lambda url: httpx.Client(base_url=url, timeout=10.0)) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["mapping", "inspect", "validate", "export", "replay"])
    p.add_argument("file", type=Path, nargs="?")
    p.add_argument("--provider", required=True, choices=sorted(ADAPTERS))
    p.add_argument("--assignments", type=Path)
    p.add_argument("--clock-basis", default="device_utc")
    p.add_argument("--offset-s", type=float, default=0.0)
    p.add_argument("--latency-s", type=float, default=0.0)
    p.add_argument("--uncertainty-s", type=float)
    p.add_argument("--source", default="wearable", choices=["wearable", "simulated", "manual"])
    p.add_argument("--out", type=Path)
    p.add_argument("--provenance", type=Path)
    p.add_argument("--status-log", type=Path)
    p.add_argument("--report", type=Path)
    p.add_argument("--url", default="http://127.0.0.1:8000")
    p.add_argument("--speed", type=replay_samples.validate_speed, default=1.0)
    try:
        args = p.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    adapter = ADAPTERS[args.provider]
    if args.command == "mapping":
        out(f"{adapter.name} mapping version {adapter.mapping.version} (FICTIONAL provider format)")
        for m in adapter.mapping.describe():
            out(f"  {m['target']:<20} <- {m['source']:<10} {m['conversion']}" + (f" {m['categories']}" if "categories" in m else "") + (f"  ({m['note']})" if "note" in m else ""))
        for k, why in adapter.mapping.unmapped.items():
            out(f"  UNMAPPED {k}: {why}")
        for k, why in adapter.mapping.quality.items():
            out(f"  quality  {k}: {why}")
        return 0
    if args.file is None or not args.file.is_file():
        out("INVALID INPUT: a readable FILE is required")
        return 2 if args.file is None else 1
    reader = adapter.reader(args.file)
    records = reader.read()
    if reader.problems:
        out(f"INVALID INPUT: {len(reader.problems)} format problems; nothing was produced.")
        for e in reader.problems[:20]:
            out(f"  - {e}")
        return 1
    if args.command == "inspect":
        by = Counter((r.device, r.kind) for r in records)
        out(f"{len(records)} records from {args.file.name} ({adapter.name}, fictional format)")
        for (dev, kind), n in sorted(by.items()):
            out(f"  {dev}: {n} {kind}")
        return 0
    if args.command == "export" and not args.out:
        out("REFUSED: export needs --out")
        return 2
    if not args.assignments:
        out("REFUSED: --assignments is required (device-to-athlete assignments are never inferred)")
        return 2
    for path in (args.out, args.provenance, args.status_log, args.report):
        if path is not None and path.exists():
            out(f"REFUSED: {path} already exists; nothing was written")
            return 1
    try:
        assignments = asg.load(args.assignments)
        clock = ClockMapping(args.clock_basis, args.offset_s, args.latency_s, args.uncertainty_s)
        res = normalize(records, adapter, assignments, clock, source=args.source)
    except (asg.AssignmentError, ValueError, OSError, json.JSONDecodeError) as exc:
        out("INVALID INPUT: nothing was produced or sent.")
        for e in getattr(exc, "errors", [str(exc)]):
            out(f"  - {e}")
        return 1
    athletes = Counter(s["athlete_id"] for s in res.samples)
    out(f"{len(records)} records -> {len(res.samples)} physiological samples for {len(athletes)} athletes ({', '.join(f'{a} {n}' for a, n in sorted(athletes.items()))}); "
        f"skipped: {', '.join(f'{k} {v}' for k, v in sorted(res.skipped.items())) or 'none'}")
    out(f"  value issues (value left null): {len(res.issues)}; conflicting duplicates: {len(res.conflicts)}; ordering: {res.ordering}")
    out(f"  device-status history: {len(res.status_history)} entries ({', '.join(f'{k} {v}' for k, v in sorted(Counter(s['event'] for s in res.status_history).items()))})")
    out(f"  clock: {clock.describe()}")
    report = {"provider": adapter.name, "fictional_format": True, "mapping": adapter.mapping.describe(), "unmapped": adapter.mapping.unmapped, "clock": clock.describe(),
              "records": len(records), "samples": len(res.samples), "skipped": res.skipped, "issues": res.issues, "conflicts": res.conflicts, "ordering": res.ordering,
              "status_history": len(res.status_history)}
    if args.command == "validate":
        for i in res.issues[:20]:
            out(f"  issue line {i.get('line')}: {i.get('field', '')} {i['reason']} (raw {i.get('raw')!r})")
        out("VALID: nothing was written or sent.")
        return 0
    recs = [{"type": "physiological", "sample": s} for s in res.samples]
    if args.out:
        write_jsonl(recs, args.out); out(f"wrote {args.out}")
    if args.provenance:
        write_jsonl(res.provenance, args.provenance); out(f"wrote {args.provenance}")
    if args.status_log:
        write_jsonl(res.status_history, args.status_log); out(f"wrote {args.status_log}")
    if args.report:
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8"); out(f"wrote {args.report}")
    if args.command == "export":
        return 0
    loaded = [replay_samples.parse_line(i + 1, json.dumps(r)) for i, r in enumerate(recs)]
    bad = [x for x in loaded if isinstance(x, list)]
    if bad:
        out(f"INVALID SAMPLES: {bad[0]}")
        return 1
    out("PACED REPLAY of a file (not a live device connection)")
    with make_client(args.url) as client:
        result = replay_samples.replay(replay_samples.order_records(loaded), client, args.speed, out=lambda _: None)
    for line in replay_samples.summarize(result, len(loaded)):
        out(line)
    return 0 if result.failed == 0 else 3


if __name__ == "__main__":
    sys.exit(main())
