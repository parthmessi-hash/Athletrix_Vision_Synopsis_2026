"""Blind reviewer annotation of a few moments from one clip (app/core/annotation.py).

USAGE
    python scripts/annotate.py blank SHEET.json --reviewer ID --out REVIEWER_SHEET.json   # labels removed: give one to each reviewer
    python scripts/annotate.py validate SHEET.json [--complete]
    python scripts/annotate.py compare A.json B.json [--json]                                # agreement, assessable counts, every disagreement
    python scripts/annotate.py adjudicate A.json B.json --decisions D.json --adjudicator ID --out ADJ.json
    python scripts/annotate.py import SHEET.json --recording ID [--engine URL] [--dry-run]   # into the engine's manual evidence
Each reviewer fills their own copy, by hand or in tools/video-review.html (Annotation sheet panel), without seeing the
other's labels or any experimental result. compare never edits a sheet; adjudicate writes a NEW sheet naming the
originals by hash. Nothing recognises passes or grades decisions automatically.
Examples: scripts/examples/annotation/ (annotation_sheet_template.json is blank; the fictional_* files are FICTIONAL).
Exit codes: 0 ok, 1 comparison found disagreements (compare only, informational), 2 invalid input, 3 engine unreachable.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.annotation import SheetError, adjudicate, blank, compare, to_observations, validate  # noqa: E402


def read(p: Path) -> dict:
    return json.loads(Path(p).read_text(encoding="utf-8"))


def write_new(p: Path, doc: dict, out) -> bool:
    if p.exists():
        out(f"{p} exists; not overwritten")
        return False
    p.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return True


def main(argv=None, out=print, client: httpx.Client | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("blank"); p.add_argument("sheet", type=Path); p.add_argument("--reviewer", required=True); p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("validate"); p.add_argument("sheet", type=Path); p.add_argument("--complete", action="store_true")
    p = sub.add_parser("compare"); p.add_argument("a", type=Path); p.add_argument("b", type=Path); p.add_argument("--json", action="store_true")
    p = sub.add_parser("adjudicate"); p.add_argument("a", type=Path); p.add_argument("b", type=Path); p.add_argument("--decisions", type=Path, required=True)
    p.add_argument("--adjudicator", required=True); p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("import"); p.add_argument("sheet", type=Path); p.add_argument("--recording", required=True); p.add_argument("--engine", default="http://127.0.0.1:8000")
    p.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "blank":
            return 0 if write_new(args.out, blank(read(args.sheet), args.reviewer), out) else 2
        if args.cmd == "validate":
            errs = validate(read(args.sheet), require_labels=args.complete)
            for e in errs:
                out(f"  {e}")
            out("valid" if not errs else f"{len(errs)} problem(s)")
            return 0 if not errs else 2
        if args.cmd == "compare":
            r = compare(read(args.a), read(args.b))
            if args.json:
                out(json.dumps(r, indent=2))
            else:
                out(f"Reviewers {r['reviewers'][0]} and {r['reviewers'][1]} on the same {sum(v['moments_asked'] for v in r['per_question'].values())} questions"
                    + (" (FICTIONAL)" if r["fictional"] else ""))
                for q, v in r["per_question"].items():
                    out(f"  {q:<15} asked {v['moments_asked']}, both assessed {v['both_assessed']}, same assessability {v['same_assessability']}, "
                        f"agreed {v['agreed_where_both_assessed']}" + (f" ({v['agreement']:.0%})" if v["agreement"] is not None else "") + (f", unlabelled {v['unlabelled_by_either']}" if v["unlabelled_by_either"] else ""))
                for d in r["disagreements"]:
                    out(f"  DISAGREE {d['moment']} {d['question']} ({d['athlete_id']}): A {json.dumps(d['a'])} | B {json.dumps(d['b'])}")
                out(r["note"])
            return 1 if r["disagreements"] else 0
        if args.cmd == "adjudicate":
            d = read(args.decisions)
            adj = adjudicate(read(args.a), read(args.b), d.get("decisions", d) if isinstance(d, dict) else d, args.adjudicator)
            if not write_new(args.out, adj, out):
                return 2
            out(f"wrote {args.out}: {len(adj['adjudication']['unresolved'])} unresolved; the original sheets are unchanged")
            return 0
        sheet = read(args.sheet)
        bodies = to_observations(sheet)
        if args.dry_run:
            for b in bodies:
                out(json.dumps(b))
            out(f"{len(bodies)} observations (dry run: nothing sent)")
            return 0
        own = client is None
        client = client or httpx.Client(timeout=15.0)
        stored, refused = 0, []
        try:
            for b in bodies:
                r = client.post(f"{args.engine.rstrip('/')}/api/v1/recordings/{args.recording}/performance", json=b)
                if r.status_code == 201:
                    stored += 1
                else:
                    refused.append(f"{b['description']}: HTTP {r.status_code} {r.text[:160]}")
        except httpx.HTTPError as exc:
            out(f"The engine is not reachable ({exc}); {stored} stored before the failure.")
            return 3
        finally:
            if own:
                client.close()
        out(f"{stored} of {len(bodies)} observations stored in {args.recording}" + ("" if not refused else "; refused: " + " | ".join(refused)))
        return 0 if not refused else 2
    except (SheetError, OSError, ValueError, KeyError) as exc:
        out(f"Error: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
