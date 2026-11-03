"""Inspect and compare experimental profiles with the engine's own parsers (app/core/profiles.py). Read-only.

USAGE
    python scripts/profile_tool.py inspect FILE [FILE ...] [--json]
    python scripts/profile_tool.py compare OLD NEW [--json]

inspect   kind, version, SHA-256, fictional flag, what the profile sets, errors and warnings; with several files, also
          the compatibility problems between them (as if configured together).
compare   the keys whose values differ between two profiles of the same kind, and whether the version was changed
          (a changed profile should carry a new version; saved reviews keep the exact text they used).

EXIT CODES: 0 valid (and compatible), 1 an error or incompatibility, 2 bad arguments or unreadable file.
"""

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.profiles import compatibility, inspect  # noqa: E402


def flatten(d, prefix=""):
    if isinstance(d, dict):
        out = {}
        for k, v in d.items():
            out.update(flatten(v, f"{prefix}{k}."))
        return out
    return {prefix.rstrip("."): d}


def main(argv: list[str] | None = None, out: Callable[[str], None] = print) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    i = sub.add_parser("inspect", help="inspect one or more profile files")
    i.add_argument("files", type=Path, nargs="+")
    i.add_argument("--json", action="store_true")
    c = sub.add_parser("compare", help="compare two profiles of the same kind")
    c.add_argument("old", type=Path)
    c.add_argument("new", type=Path)
    c.add_argument("--json", action="store_true")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    files = args.files if args.command == "inspect" else [args.old, args.new]
    try:
        found = [inspect(f.read_bytes(), f.name) for f in files]
    except OSError as exc:
        out(f"cannot read: {exc}")
        return 2
    if args.command == "inspect":
        problems = compatibility({r["kind"]: r for r in found if r["kind"]}) if len(found) > 1 else []
        if args.json:
            out(json.dumps({"profiles": found, "compatibility": problems}, indent=2))
        else:
            for r in found:
                out(f"{r['name']}: {r['kind'] or 'unknown kind'} version {r['version']!r}, sha256 {r['sha256'][:16]}…, {'FICTIONAL' if r['fictional'] else 'not marked fictional'}, {'valid' if r['valid'] else 'INVALID'}")
                for k, v in r["summary"].items():
                    out(f"  {k}: {json.dumps(v)[:200]}")
                for e in r["errors"]:
                    out(f"  ERROR: {e}")
                for w in r["warnings"]:
                    out(f"  warning: {w}")
            for p in problems:
                out(f"COMPATIBILITY: {p}")
        return 0 if all(r["valid"] for r in found) and not problems else 1
    old, new = found
    if old["kind"] != new["kind"]:
        out(f"different kinds: {old['kind']} and {new['kind']}")
        return 1
    a, b = flatten(old["summary"]), flatten(new["summary"])
    diff = {k: {"old": a.get(k), "new": b.get(k)} for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)}
    result = {"kind": old["kind"], "old": {"version": old["version"], "sha256": old["sha256"]}, "new": {"version": new["version"], "sha256": new["sha256"]},
              "same_content": old["sha256"] == new["sha256"], "version_changed": old["version"] != new["version"], "differences": diff}
    if args.json:
        out(json.dumps(result, indent=2))
    else:
        out(f"{old['kind']}: {old['version']} -> {new['version']} ({'same bytes' if result['same_content'] else 'different bytes'})")
        for k, v in diff.items():
            out(f"  {k}: {v['old']} -> {v['new']}")
        if diff and not result["version_changed"]:
            out("WARNING: the settings differ but the version did not change; give a changed profile a new version")
    return 0 if old["valid"] and new["valid"] and (not diff or result["version_changed"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
