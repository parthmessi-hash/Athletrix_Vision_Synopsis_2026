"""Inventory and remove stored research data in one local Athletrix database file. Dry run by default.

USAGE
    python scripts/local_data.py inventory --database DB [--json]
    python scripts/local_data.py remove-recording REC_ID --database DB                       # dry run: exactly what would go
    python scripts/local_data.py remove-recording REC_ID --database DB --execute --confirm REC_ID
    python scripts/local_data.py remove-athlete ATHLETE_ID --database DB [--include-device-status DEVICE_ID ...]
    python scripts/local_data.py remove-athlete ATHLETE_ID --database DB --execute --confirm ATHLETE_ID
    add --json for a machine-readable report

SCOPES (see app/store/removal.py for the exact relationships)
  remove-recording  the recording, its roster snapshot, recorded samples, saved periods, configuration snapshot and
                    import provenance. Rolling history, the registry and other recordings stay.
  remove-athlete    the registry entry, rolling samples and, in every recording, that athlete's membership, recorded
                    samples and saved periods. Shared recordings stay valid for the other athletes; a recording left with
                    no roster member is removed entirely. Device status is keyed by device id and is only reported,
                    unless a device is named with --include-device-status and no other athlete's samples name it.

SAFETY
  The database is named explicitly; there is no default, and a missing file is never created. A dry run opens the file
  read-only and changes no data (SQLite may create empty -wal/-shm sidecar files next to a WAL database it opens). --execute requires --confirm with the exact target id. Execution takes SQLite's
  exclusive lock, which SQLite refuses while another connection (such as a running engine) has the file open, so stop
  the engine first; it then recomputes the plan and applies it in one transaction that rolls back completely on any
  failure. Unknown targets change nothing. Active recordings are refused.

WHAT THIS IS NOT
  Logical deletion in this one file, not secure erasure and not a complete consent-withdrawal process. It cannot reach
  exported files or packages already copied elsewhere (including other engines that imported them), camera footage,
  adapter logs, backups, snapshots or external systems, or every physical trace on the storage medium.

EXIT CODES
  0 inventory shown, clean dry run, or removal done · 1 unknown target, refused, database in use, or failed · 2 bad arguments
"""

import argparse
import json
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.store.removal import CANNOT_REMOVE, DatabaseInUse, execute, inventory, open_read_only, plan_athlete, plan_recording  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    i = sub.add_parser("inventory", help="row counts, recordings and athlete ids in the database (read-only)")
    i.add_argument("--database", type=Path, required=True)
    i.add_argument("--json", action="store_true")
    for name, what in (("remove-recording", "recording id"), ("remove-athlete", "athlete id")):
        r = sub.add_parser(name, help=f"plan (default) or execute the removal of one {what}")
        r.add_argument("target", help=what)
        r.add_argument("--database", type=Path, required=True)
        r.add_argument("--execute", action="store_true", help="apply the removal (requires --confirm with the exact id)")
        r.add_argument("--confirm", help="the exact target id again, required with --execute")
        r.add_argument("--json", action="store_true")
        if name == "remove-athlete":
            r.add_argument("--include-device-status", action="append", default=[], metavar="DEVICE_ID", help="also remove this device's latest status (only if no other athlete's samples name it)")
    return p


def show_plan(report: dict, executed: bool, attempted: bool, out: Callable[[str], None]) -> None:
    head = "REMOVED" if executed else ("DRY RUN (nothing was changed)" if report["executable"] else "NOT EXECUTABLE (nothing was changed)")
    out(f"{head}: {report['scope']} {report['target']}")
    if not report["found"]:
        out(f"  No {report['scope']} {report['target']!r} is stored in this database; nothing {'was' if attempted else 'would be'} changed.")
        return
    verb = "deleted" if executed else "would delete"
    for d in report["deletes"]:
        out(f"  {verb} {d['count']:>7} rows from {d['table']:<34} {d['detail']}")
    for u in report["updates"]:
        out(f"  {'updated' if executed else 'would update'} {u['table']}: {u['detail']}")
    out(f"  total: {report['rows_to_delete']} rows")
    for k in report["kept"]:
        out(f"  kept: {k}")
    for r in report["refusals"]:
        out(f"  REFUSED: {r}")
    for lim in report["limitations"]:
        out(f"  limitation: {lim}")
    out("  This tool cannot remove: " + "; ".join(CANNOT_REMOVE) + ".")


def main(argv: list[str] | None = None, *, out: Callable[[str], None] = print, checkpoint=None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    path = args.database.expanduser()
    if args.command == "inventory":
        try:
            c = open_read_only(path)
        except (FileNotFoundError, sqlite3.Error) as exc:
            out(f"ERROR: {exc}")
            return 1
        try:
            inv = inventory(c)
        finally:
            c.close()
        if args.json:
            out(json.dumps(inv, indent=2))
        else:
            out(f"Database {path} (read-only inventory)")
            for t, n in inv["tables"].items():
                out(f"  {t:<34} {n:>8} rows")
            for r in inv["recordings"]:
                out(f"  recording {r['id']} \"{r['name']}\" {r['status']} · roster {', '.join(r['roster']) or '(none)'} · {r.get('recording_physiological_samples', 0)} physiological, {r.get('recording_tracking_samples', 0)} tracking, {r.get('recording_periods', 0)} periods")
            for a in inv["athletes"]:
                out(f"  athlete {a['id']} ({'registered' if a['registered'] else 'not registered'}) · rolling {a.get('physiological_samples', 0)}/{a.get('tracking_samples', 0)} · in {a.get('recording_athletes', 0)} recordings · {a.get('recording_periods', 0)} saved periods")
            out("  " + inv["note"])
        return 0

    if args.command == "remove-recording":
        make = lambda c: plan_recording(c, args.target)  # noqa: E731
    else:
        devices = tuple(args.include_device_status)
        make = lambda c: plan_athlete(c, args.target, devices)  # noqa: E731
    if args.execute and args.confirm != args.target:
        out(f"REFUSED: --execute needs --confirm {args.target} (the exact id); nothing was changed.")
        return 2
    try:
        if args.execute:
            plan = execute(path, make, checkpoint=checkpoint)
        else:
            c = open_read_only(path)
            try:
                plan = make(c)
            finally:
                c.close()
    except DatabaseInUse as exc:
        out(f"REFUSED: {exc}")
        return 1
    except FileNotFoundError as exc:
        out(f"ERROR: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001  the transaction was rolled back
        out(f"REMOVAL FAILED and was rolled back completely ({type(exc).__name__}: {exc}). Nothing was changed.")
        return 1
    report = plan.report()
    executed = args.execute and report["executable"]
    if args.json:
        out(json.dumps({**report, "executed": executed}, indent=2))
    else:
        show_plan(report, executed, args.execute, out)
    return 0 if report["executable"] else 1


if __name__ == "__main__":
    sys.exit(main())
