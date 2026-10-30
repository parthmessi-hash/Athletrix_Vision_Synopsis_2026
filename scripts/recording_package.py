"""Inspect, validate and import Athletrix portable recording packages on this computer.

A package (format athletrix-recording-package, version 2; version 1 is still read) is downloaded from a stopped recording with
GET /api/v1/recordings/{id}/package.zip. This tool reads it locally; nothing is uploaded anywhere.

USAGE
    python scripts/recording_package.py inspect PACKAGE.zip                        # validate fully and summarise; touches no database
    python scripts/recording_package.py import PACKAGE.zip --database DEST.db --dry-run   # validate and check the destination read-only
    python scripts/recording_package.py import PACKAGE.zip --database DEST.db             # import as a new stopped recording
    python scripts/recording_package.py import PACKAGE.zip --database DEST.db --allow-duplicate   # import a second, separate copy
    add --json for a machine-readable summary

WHAT IMPORT DOES
  The destination database is named explicitly (--database); there is no default. The whole package is validated before
  anything is written: supported version, required members only, checksums and sizes, counts, every sample against the
  engine's own models, roster membership, no duplicate or conflicting samples for one athlete, stream and instant,
  every saved period against the effort rules, and no sample timestamped more than the live skew allowance after the
  recording stopped. The import then writes, in one transaction, a new stopped recording with the original name, start
  and stop times, the roster snapshot, every sample as normalised in the package, and every saved period with its
  original label and exact bounds, all under new ids, plus a provenance row that maps the new ids to the original ones
  (shown by GET /recordings/{id} as `imported`). Any failure rolls everything back.

  The athlete registry, rolling history, device status and any active recording are never touched, and nothing is
  overwritten or merged. Importing a package that was already imported into that database is refused; --allow-duplicate
  imports it again as a separate recording with new ids. Samples keep their recorded timestamps: an import is historical
  data, not live ingestion, so nothing is rebased and arrival-time rules are not re-applied (beyond the stop-time check).

  The archive is read in memory by exact member name; nothing is extracted to disk, and oversized archives are refused.

  Stop the destination engine before importing into its database, or import into a separate file. The import takes the
  database's maintenance lease (app/core/db_lease.py) and refuses while an engine of this version has the file open; an
  engine started during the import refuses to open it until the import finishes.

EXIT CODES
  0 success (or a clean dry run) · 1 the package is invalid or the import was refused or failed · 2 bad arguments
"""

import argparse
import json
import sqlite3
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.recording_package import MAX_PACKAGE_BYTES, PackageError, read_package  # noqa: E402
from app.core.db_lease import Lease, LeaseUnavailable  # noqa: E402
from app.db import Database  # noqa: E402
from app.store.recording_import import AlreadyImported, import_package, previous_imports  # noqa: E402


def load(path: Path) -> bytes:
    if not path.is_file():
        raise PackageError([f"{path} is not a file"])
    if path.stat().st_size > MAX_PACKAGE_BYTES:
        raise PackageError([f"{path} is larger than this reader processes (2 GiB)"])
    return path.read_bytes()


def describe(summary: dict, out: Callable[[str], None]) -> None:
    r = summary["recording"]
    out(f"Package: {summary['format']} version {summary['format_version']}, generated {summary['generated_at']}, SHA-256 {summary['package_sha256'][:16]}…")
    out(f"Recording: \"{r['name']}\" (original id {r['id']}) · recorded {r['started_at']} to {r['stopped_at']} (backend clock while recording)")
    c = summary["counts"]
    out(f"Contents: {len(summary['athletes'])} athletes · {c['physiological_samples']} physiological and {c['tracking_samples']} tracking samples · {c['periods']} saved periods · {c.get('context_entries', 0)} coach-context entries")
    for a in summary["athletes"]:
        def b(s):
            return f"{s['count']} ({s['bounds']['earliest']} to {s['bounds']['latest']})" if s["bounds"] else "0"
        out(f"  {a['id']} · {a['name']} #{a['jersey_number']} · {a['sport']} · physiological {b(a['physiological'])} · tracking {b(a['tracking'])} · periods {a['periods']}")
    s = summary.get("settings_at_export") or {}
    out(f"Settings at export (informational; the destination applies its own): max gap {s.get('distance_max_gap_seconds')} s, min confidence {s.get('distance_min_confidence')}, high-speed threshold {s.get('high_speed_threshold_mps')} m/s")
    cfg = summary.get("configuration_at_start") or {}
    snap = cfg.get("snapshot")
    if snap:
        m, r, e = snap["movement"], snap["retention"], snap["engine"]
        supplied = " · operator-supplied adapter information present (not verified)" if snap.get("operator_supplied") else ""
        out(f"Configuration at recording start (captured by the source engine): {e['application']} {e['version']}, code revision {e['code_revision'] or 'unknown'} · "
            f"max gap {m['distance_max_gap_seconds']} s, min confidence {m['distance_min_confidence']}, high-speed {m['high_speed_threshold_mps']} m/s · "
            f"caps {r['physiological_retention_samples']}/{r['tracking_retention_samples']}{supplied}")
    else:
        out("Configuration at recording start: unavailable (not recorded for this recording, or a version 1 package); it is not reconstructed.")


def destination_state(path: Path, package_sha256: str) -> dict:
    """Read-only look at the destination: does it exist, and was this package imported into it before."""
    if not path.exists():
        return {"exists": False, "previous_imports": []}
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {"exists": True, "previous_imports": previous_imports(con, package_sha256)}
    finally:
        con.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    i = sub.add_parser("inspect", help="validate a package fully and summarise it; no database is opened")
    i.add_argument("package", type=Path)
    i.add_argument("--json", action="store_true")
    m = sub.add_parser("import", help="import a package into an explicitly named database as a new stopped recording")
    m.add_argument("package", type=Path)
    m.add_argument("--database", type=Path, required=True, help="destination SQLite file (required; created if missing unless --dry-run)")
    m.add_argument("--dry-run", action="store_true", help="validate everything and check the destination read-only; write nothing")
    m.add_argument("--allow-duplicate", action="store_true", help="import again although this package was already imported there (a separate recording)")
    m.add_argument("--json", action="store_true")
    return p


def main(argv: list[str] | None = None, *, out: Callable[[str], None] = print, now: Callable[[], datetime] = lambda: datetime.now(UTC), checkpoint=None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    try:
        pkg = read_package(load(args.package))
    except PackageError as exc:
        if getattr(args, "json", False):
            out(json.dumps({"ok": False, "errors": exc.errors}, indent=2))
        else:
            out(f"INVALID PACKAGE {args.package}: nothing was imported.")
            for e in exc.errors:
                out(f"  - {e}")
        return 1
    summary = pkg.summary()
    if args.command == "inspect":
        if args.json:
            out(json.dumps({"ok": True, **summary}, indent=2))
        else:
            out("VALID PACKAGE (checked in memory; no database was opened)")
            describe(summary, out)
        return 0
    dest = args.database.expanduser()
    if args.dry_run:
        state = destination_state(dest, pkg.package_sha256)
        refused = bool(state["previous_imports"]) and not args.allow_duplicate
        result = {"ok": not refused, "dry_run": True, "destination": {"exists": state["exists"], "previous_imports": state["previous_imports"]}, **summary}
        if args.json:
            out(json.dumps(result, indent=2))
        else:
            out("DRY RUN: the package is valid; nothing was written" + ("" if state["exists"] else " (the destination file does not exist yet; an import would create it)") + ".")
            describe(summary, out)
            if refused:
                out(f"An import would be REFUSED: this package was already imported there as {', '.join(state['previous_imports'])} (use --allow-duplicate for a separate copy).")
            else:
                out("An import would create one new stopped recording with new ids; the registry, rolling history and any active recording would not change.")
        return 1 if refused else 0
    try:
        lease = Lease(dest, exclusive=True)   # refuses while an engine (or another tool) uses this file; see app/core/db_lease.py
    except LeaseUnavailable as exc:
        out(f"REFUSED: {exc}")
        return 1
    db = Database(dest, lease=False)
    try:
        res = import_package(db, pkg, now(), allow_duplicate=args.allow_duplicate, checkpoint=checkpoint)
    except AlreadyImported as exc:
        out(f"REFUSED: {exc}. Nothing was written.")
        return 1
    except Exception as exc:  # noqa: BLE001  the transaction was rolled back
        out(f"IMPORT FAILED and was rolled back completely ({type(exc).__name__}: {exc}). Nothing was written.")
        return 1
    finally:
        db.close()
        lease.release()
    result = {"ok": True, "recording_id": res.recording_id, "source_recording_id": res.source_recording_id, "period_id_map": res.period_id_map, "samples": res.samples, "imported_at": res.imported_at.isoformat()}
    if args.json:
        out(json.dumps({**result, "package": summary}, indent=2))
    else:
        out(f"IMPORTED as recording {res.recording_id} (original {res.source_recording_id}) into {dest}: {res.samples['physiological']} physiological and {res.samples['tracking']} tracking samples, {len(res.period_id_map)} saved periods.")
        for old, new in res.period_id_map.items():
            out(f"  period {old} -> {new}")
        out("Review it in that engine: GET /api/v1/recordings/" + res.recording_id + " (its `imported` field keeps this mapping).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
