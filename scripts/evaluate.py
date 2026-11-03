"""Evaluation workbench: compare engine outputs with INDEPENDENT reference annotations, keeping each question separate.

USAGE
    python scripts/evaluate.py make-fictional --out DIR          # a FICTIONAL dataset to exercise the workbench (labelled fictional)
    python scripts/evaluate.py run DATASET.json --out DIR [--split development|held_out|all]
                               [--index-profile A.json [--index-profile B.json]] [--substitution-profile S.json] [--rubric R.json]

DATASET (format "athletrix-evaluation-dataset", version 1; paths relative to the dataset file):
    dataset_id, fictional (true for made-up data), provenance, code_revision (as supplied),
    cases: [{case_id, split: "development" | "held_out", package: PATH (a recording package), label}],
    references: {camera_positions: [{case_id, file, reviewer_id, source}],   JSON Lines {athlete_id, timestamp, x, y}
                 performance_labels: [{file, reviewer_id, source}],          JSON list {case_id, item_id, athlete_id, kind, period_start, period_end, ...counts or outcome}
                 coach_judgments: [{file, reviewer_id, source}]},            JSON list {case_id, athlete_id, at, judgment}
    tuning_log: [{date, what, split_used}]                                   every choice made while looking at results

QUESTIONS (reported separately; agreement on one says nothing about another)
    camera        engine tracking samples vs reference positions: a reference point is matched to the sample of the same
                  athlete nearest in time within --match-window-s (default 0.1 s); unmatched points are coverage gaps,
                  reported next to the position errors. Points of one athlete in one case are repeated observations of the
                  same person, not independent participants.
    performance   engine performance observations vs reference labels, per reviewer: an item matches the engine observation
                  of the same athlete and kind whose period overlaps it most (at least half of the shorter period). Counts
                  and outcomes are compared; items labelled by several reviewers are listed with every disagreement.
                  Nothing is adjudicated away.
    profiles      two physiological index profiles on the same cases: availability and value differences. A comparison,
                  not a validation: neither profile is shown to measure anything.
    substitution  experimental outputs (context-aware rules and the score-only variant, on identical cases and times) vs each
                  coach's independently supplied judgment. Agreement with a coach is not physiological or performance
                  accuracy, and fictional judgments are labelled fictional.
SPLITS: results are reported per split. Do not choose a profile on the held-out split and keep calling it held out: if the
tuning log shows a choice made with held_out, the report says the held-out split is compromised and new examples are needed.
OUTPUT: DIR/report.md (readable) and DIR/results.json (machine-readable). DIR must not exist.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import random
import statistics
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_spec = importlib.util.spec_from_file_location("product_demo_eval", ROOT / "scripts" / "product_demo.py")
demo = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(demo)

FORMAT = "athletrix-evaluation-dataset"
JUDGMENTS = ("keep", "consider_replacement", "review_later", "insufficient_evidence")


def parse_t(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def stats(values: list[float]) -> dict | None:
    v = sorted(abs(x) for x in values)
    if not v:
        return None
    return {"n": len(v), "mean": round(statistics.fmean(v), 4), "median": round(statistics.median(v), 4), "p95": round(v[min(len(v) - 1, int(0.95 * len(v)))], 4), "max": round(v[-1], 4)}


# ---- loading ----------------------------------------------------------------------------------------------------------------

def load_dataset(path: Path, split: str) -> dict:
    doc = json.loads(path.read_text())
    if doc.get("format") != FORMAT or doc.get("format_version") != 1:
        raise ValueError(f"{path} is not an {FORMAT} version 1 file")
    for c in doc["cases"]:
        if c["split"] not in ("development", "held_out"):
            raise ValueError(f"case {c['case_id']}: split must be development or held_out")
    doc["_base"] = path.parent
    doc["_cases"] = [c for c in doc["cases"] if split == "all" or c["split"] == split]
    return doc


class Cases:
    """Every case's package imported into one isolated temporary engine database (never a development database)."""

    def __init__(self, doc: dict, profiles: dict[str, Path]) -> None:
        from app.core.recording_package import read_package  # noqa: PLC0415
        from app.db import Database  # noqa: PLC0415
        from app.store.recording_import import import_package  # noqa: PLC0415
        self.tmp = tempfile.TemporaryDirectory(prefix="athletrix-eval-")
        db_path = Path(self.tmp.name) / "eval.db"
        db = Database(db_path)
        self.packages, self.rec = {}, {}
        for c in doc["_cases"]:
            data = (doc["_base"] / c["package"]).read_bytes()
            pkg = read_package(data)
            self.packages[c["case_id"]] = {"pkg": pkg, "sha256": hashlib.sha256(data).hexdigest()}
            self.rec[c["case_id"]] = import_package(db, pkg, parse_t(pkg.manifest["generated_at"]), allow_duplicate=True).recording_id
        db.close()
        self.engine = demo.Engine(db_path, profiles)

    def get(self, url, **params):
        return self.engine.get(url, **params)

    def reconfigure(self, profiles: dict[str, Path]) -> None:
        path = Path(self.engine.db.path)
        self.engine.close()
        self.engine = demo.Engine(path, profiles)

    def close(self) -> None:
        self.engine.close()
        self.tmp.cleanup()


# ---- the four questions ---------------------------------------------------------------------------------------------------

def camera(doc: dict, cases: Cases, window_s: float) -> dict:
    out = {"rule": f"nearest engine sample of the same athlete within ±{window_s} s; unmatched reference points are coverage gaps", "by_reference": []}
    for ref in doc["references"].get("camera_positions", []):
        if ref["case_id"] not in cases.packages:
            continue
        pkg = cases.packages[ref["case_id"]]["pkg"]
        rows = [json.loads(line) for line in (doc["_base"] / ref["file"]).read_text().splitlines() if line.strip()]
        errs, unmatched, athletes = [], 0, Counter()
        for r in rows:
            athletes[r["athlete_id"]] += 1
            t = parse_t(r["timestamp"])
            cands = [s for s in pkg.samples.get(r["athlete_id"], {}).get("tracking", []) if abs((s.timestamp - t).total_seconds()) <= window_s]
            if not cands:
                unmatched += 1
                continue
            s = min(cands, key=lambda s: abs((s.timestamp - t).total_seconds()))
            errs.append(((s.x_position - r["x"]) ** 2 + (s.y_position - r["y"]) ** 2) ** 0.5)
        out["by_reference"].append({"case_id": ref["case_id"], "reviewer_id": ref["reviewer_id"], "source": ref.get("source"), "reference_points": len(rows),
                                    "matched": len(errs), "unmatched_coverage_gaps": unmatched, "coverage": round(len(errs) / len(rows), 4) if rows else None,
                                    "position_error_m": stats(errs), "athletes": dict(athletes),
                                    "note": f"{len(rows)} points from {len(athletes)} athletes: repeated observations of the same people, not {len(rows)} independent participants"})
    return out


def overlap(a0, a1, b0, b1) -> float:
    return max(0.0, (min(a1, b1) - max(a0, b0)).total_seconds())


def performance(doc: dict, cases: Cases) -> dict:
    by_reviewer, items = [], {}
    for ref in doc["references"].get("performance_labels", []):
        labels = [x for x in json.loads((doc["_base"] / ref["file"]).read_text()) if x["case_id"] in cases.packages]
        compared, unmatched, exact, diffs = 0, 0, 0, []
        for lab in labels:
            items.setdefault((lab["case_id"], lab["item_id"]), []).append({**lab, "reviewer_id": ref["reviewer_id"]})
            obs = [o for o in cases.packages[lab["case_id"]]["pkg"].performance if o.athlete_id == lab["athlete_id"] and o.kind == lab["kind"] and o.assessment == "assessed"]
            a0, a1 = parse_t(lab["period_start"]), parse_t(lab["period_end"])
            best = max(obs, key=lambda o: overlap(o.period_start, o.period_end, a0, a1), default=None)
            if best is None or overlap(best.period_start, best.period_end, a0, a1) < 0.5 * min((a1 - a0).total_seconds(), (best.period_end - best.period_start).total_seconds()):
                unmatched += 1
                continue
            compared += 1
            if lab["kind"] == "pass":
                rate_e, rate_r = best.completions / best.attempts, lab["completions"] / lab["attempts"]
                same = (best.attempts, best.completions) == (lab["attempts"], lab["completions"])
            elif lab["kind"] in ("decision", "positioning"):
                rate_e, rate_r = best.rated_good / best.opportunities, lab["rated_good"] / lab["opportunities"]
                same = (best.opportunities, best.rated_good, best.rated_acceptable) == (lab["opportunities"], lab["rated_good"], lab["rated_acceptable"])
            else:
                rate_e = rate_r = None
                same = best.outcome == lab.get("outcome")
            exact += same
            if rate_e is not None:
                diffs.append(rate_e - rate_r)
        by_reviewer.append({"reviewer_id": ref["reviewer_id"], "source": ref.get("source"), "labels": len(labels), "compared": compared, "unmatched": unmatched,
                            "exact_agreement": exact, "rate_difference": stats(diffs)})
    multi = {k: v for k, v in items.items() if len({x["reviewer_id"] for x in v}) > 1}
    disagreements = []
    for (case, item), labs in sorted(multi.items()):
        keys = [k for k in ("attempts", "completions", "opportunities", "rated_good", "rated_acceptable", "rated_poor", "outcome") if any(k in x for x in labs)]
        if len({json.dumps({k: x.get(k) for k in keys}, sort_keys=True) for x in labs}) > 1:
            disagreements.append({"case_id": case, "item_id": item, "labels": {x["reviewer_id"]: {k: x.get(k) for k in keys} for x in labs}})
    return {"rule": "match: same athlete and kind, the overlapping period covering at least half of the shorter one", "by_reviewer": by_reviewer,
            "items_with_several_reviewers": len(multi), "disagreements_between_reviewers": disagreements,
            "note": "disagreements are kept as they are; no reviewer is treated as the truth"}


def profiles(doc: dict, cases: Cases, index_profiles: list[Path], base: dict) -> dict | None:
    if len(index_profiles) < 2:
        return None
    runs = {}
    for prof in index_profiles[:2]:
        cases.reconfigure({**base, "PHYSIOLOGICAL_INDEX_PROFILE_PATH": prof})
        vals = {}
        for case, rid in cases.rec.items():   # the same instants for both profiles: every 60 s after the start
            rec = cases.packages[case]["pkg"].recording
            t0, t1 = parse_t(rec["started_at"]), parse_t(rec["stopped_at"])
            times = [t0 + timedelta(seconds=60 * k) for k in range(1, int((t1 - t0).total_seconds() // 60) + 1)]
            for a in cases.packages[case]["pkg"].athletes:
                for t in times:
                    stamp = t.isoformat().replace("+00:00", "Z")
                    vals[(case, a.id, stamp)] = cases.get(f"/recordings/{rid}/athletes/{a.id}/physiological-index", at=stamp).get("value")
        runs[prof.name] = vals
    (na, a), (nb, b) = runs.items()
    keys = sorted(set(a) | set(b))
    both = [k for k in keys if a.get(k) is not None and b.get(k) is not None]
    cases.reconfigure(base)
    return {"profiles": [na, nb], "points": len(keys), "available": {na: sum(a.get(k) is not None for k in keys), nb: sum(b.get(k) is not None for k in keys)},
            "both_available": len(both), "value_difference": stats([a[k] - b[k] for k in both]),
            "note": "a comparison of two configurations on the same data; it does not show that either measures fatigue"}


def substitution(doc: dict, cases: Cases) -> dict:
    out = {"by_reviewer": [], "note": "agreement with a coach's judgment is not physiological or performance accuracy; outputs and judgments are compared on identical cases and times"}
    from app.api import review as api  # noqa: PLC0415
    from app.core.substitution import substitution_review  # noqa: PLC0415
    from app.db import get_database  # noqa: PLC0415
    from app.store.recordings import recording_store  # noqa: PLC0415
    for ref in doc["references"].get("coach_judgments", []):
        rows = [x for x in json.loads((doc["_base"] / ref["file"]).read_text()) if x["case_id"] in cases.rec]
        table = {"context_aware": Counter(), "score_only": Counter()}
        agree = {"context_aware": 0, "score_only": 0}
        unavailable = {"context_aware": 0, "score_only": 0}
        details = []
        for j in rows:
            rid = cases.rec[j["case_id"]]
            with get_database().read():
                rec = recording_store.get(rid)
                inp, _ = api.gather(rec, j["athlete_id"], parse_t(j["at"]), api.parse_docs(api.configured_docs()), "retrospective")
            row = {"case_id": j["case_id"], "athlete_id": j["athlete_id"], "at": j["at"], "coach": j["judgment"]}
            for variant in ("context_aware", "score_only"):
                inp.rule_variant = variant
                o = substitution_review(inp)["outcome"]
                row[variant] = o
                table[variant][(o, j["judgment"])] += 1
                agree[variant] += o == j["judgment"]
                unavailable[variant] += o == "insufficient_evidence"
            details.append(row)
        out["by_reviewer"].append({"reviewer_id": ref["reviewer_id"], "source": ref.get("source"), "judgments": len(rows), "agreement": agree,
                                   "insufficient_evidence_outputs": unavailable,
                                   "confusion": {v: {f"{o} | coach {c}": n for (o, c), n in sorted(t.items())} for v, t in table.items()}, "cases": details})
    return out


# ---- run ----------------------------------------------------------------------------------------------------------------------

def run(args, out) -> int:
    doc = load_dataset(args.dataset, args.split)
    base = {"PHYSIOLOGICAL_INDEX_PROFILE_PATH": Path(args.index_profile[0]) if args.index_profile else demo.PROFILE_FILES["PHYSIOLOGICAL_INDEX_PROFILE_PATH"],
            "SUBSTITUTION_PROFILE_PATH": Path(args.substitution_profile) if args.substitution_profile else demo.PROFILE_FILES["SUBSTITUTION_PROFILE_PATH"],
            "PERFORMANCE_RUBRIC_PATH": Path(args.rubric) if args.rubric else demo.PROFILE_FILES["PERFORMANCE_RUBRIC_PATH"]}
    saved_env = {k: os.environ.get(k) for k in base}
    cases = Cases(doc, base)
    try:
        held_tuned = [t for t in doc.get("tuning_log", []) if t.get("split_used") in ("held_out", "all")]
        try:
            rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=5).stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            rev = None
        results = {"tool": "scripts/evaluate.py", "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"), "dataset_id": doc["dataset_id"], "fictional": bool(doc.get("fictional")),
                   "provenance": doc.get("provenance"), "dataset_code_revision": doc.get("code_revision"), "engine_code_revision": rev, "split": args.split,
                   "cases": [{"case_id": c["case_id"], "split": c["split"], "label": c.get("label"), "package_sha256": cases.packages[c["case_id"]]["sha256"]} for c in doc["_cases"]],
                   "configuration": {k: {"file": v.name, "sha256": hashlib.sha256(v.read_bytes()).hexdigest()} for k, v in base.items()},
                   "held_out_status": ("COMPROMISED: the tuning log records choices made with the held-out split; collect new examples before calling anything held out"
                                       if held_tuned else "no tuning with the held-out split is recorded"),
                   "tuning_log": doc.get("tuning_log", []),
                   "camera": camera(doc, cases, args.match_window_s), "performance": performance(doc, cases),
                   "profiles": profiles(doc, cases, [Path(p) for p in args.index_profile or []], base), "substitution": substitution(doc, cases)}
    finally:
        cases.close()
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        from app.core.config import get_settings  # noqa: PLC0415
        get_settings.cache_clear()
    args.out.mkdir(parents=True)
    (args.out / "results.json").write_text(json.dumps(results, indent=2, default=str))
    (args.out / "report.md").write_text(report(results))
    out(f"wrote {args.out / 'report.md'} and results.json" + (" · FICTIONAL dataset" if results["fictional"] else ""))
    return 0


def report(r: dict) -> str:
    L = [f"# Evaluation report: {r['dataset_id']}", "", ("**FICTIONAL dataset: made-up data and made-up reference judgments. This exercises the workbench; it evaluates nothing real.**" if r["fictional"] else ""),
         "", f"Generated {r['generated_at']} by {r['tool']} (engine code {r['engine_code_revision'] or 'unknown'}). Split: {r['split']}. Held-out status: {r['held_out_status']}.", "",
         "| Case | Split | Package SHA-256 |", "|---|---|---|"] + [f"| {c['case_id']} | {c['split']} | `{c['package_sha256'][:16]}…` |" for c in r["cases"]] + ["", "Configuration: " + "; ".join(f"{k} {v['file']} (`{v['sha256'][:12]}…`)" for k, v in r["configuration"].items()), "",
         "## 1. Camera positions", r["camera"]["rule"], ""]
    for c in r["camera"]["by_reference"]:
        e = c["position_error_m"] or {}
        L.append(f"- {c['case_id']}, reviewer {c['reviewer_id']} ({c['source']}): {c['matched']} of {c['reference_points']} reference points matched (coverage {c['coverage']}), {c['unmatched_coverage_gaps']} coverage gaps; "
                 f"position error median {e.get('median')} m, p95 {e.get('p95')} m, max {e.get('max')} m. {c['note']}.")
    L += ["", "## 2. Performance evidence vs reference labels", r["performance"]["rule"], ""]
    for b in r["performance"]["by_reviewer"]:
        L.append(f"- reviewer {b['reviewer_id']} ({b['source']}): {b['compared']} of {b['labels']} labels compared ({b['unmatched']} unmatched), {b['exact_agreement']} exact agreements; rate difference {b['rate_difference']}")
    L.append(f"- {r['performance']['items_with_several_reviewers']} items labelled by several reviewers; {len(r['performance']['disagreements_between_reviewers'])} disagreements kept as they are:")
    for d in r["performance"]["disagreements_between_reviewers"]:
        L.append(f"  - {d['case_id']} {d['item_id']}: " + "; ".join(f"{k}: {json.dumps(v)}" for k, v in d["labels"].items()))
    L += ["", "## 3. Physiological index profiles compared", json.dumps(r["profiles"], indent=1) if r["profiles"] else "Not run (give --index-profile twice).", "",
          "## 4. Substitution outputs vs coach judgments", r["substitution"]["note"], ""]
    for b in r["substitution"]["by_reviewer"]:
        L.append(f"- reviewer {b['reviewer_id']} ({b['source']}): {b['judgments']} judgments; agreement context-aware {b['agreement']['context_aware']}, score-only {b['agreement']['score_only']}; "
                 f"insufficient-evidence outputs {b['insufficient_evidence_outputs']}")
        for c in b["cases"]:
            L.append(f"  - {c['case_id']} {c['athlete_id']} at {c['at']}: coach {c['coach']} · context-aware {c['context_aware']} · score-only {c['score_only']}")
    L += ["", "## Tuning log", json.dumps(r["tuning_log"], indent=1) or "none", ""]
    return "\n".join(L)


# ---- a fictional dataset --------------------------------------------------------------------------------------------------------

def make_fictional(out_dir: Path, out) -> int:
    """Two fictional matches (development and held out), fictional camera references, two fictional performance reviewers
    who disagree on some items, and two fictional coaches' judgments of the demo scenarios. Every file says FICTIONAL."""
    out_dir.mkdir(parents=True)
    rng = random.Random("athletrix-eval-fictional")
    pkgs = {"match-dev": demo.build(), "match-held": demo.build("held-out")}
    from app.core.recording_package import build_package, read_package  # noqa: PLC0415
    from app.models import TrackingSample  # noqa: PLC0415
    # add fictional camera samples for two athletes to the development match: truth + noise, with a gap
    pkg = read_package(pkgs["match-dev"])
    truth = {}
    for aid in ("p01", "p02"):
        rows = []
        for k in range(0, 600):
            s = 600 + k * 0.5
            x, y = 10 + 0.02 * k + (0 if aid == "p01" else 5), 20 + 3 * ((k // 40) % 2)
            truth.setdefault(aid, []).append((s, x, y))
            if 300 <= k < 340:   # a fictional assignment loss: no samples
                continue
            rows.append(TrackingSample(athlete_id=aid, timestamp=demo.at(s), x_position=round(x + rng.gauss(0, 0.3), 3), y_position=round(y + rng.gauss(0, 0.3), 3), speed_mps=1.0,
                                       acceleration_mps2=0.0, tracking_confidence=0.9, source="camera", device_id="fictional-cam", segment_id=f"seg-{aid}-{k // 300}", value_basis="estimated"))
        pkg.samples[aid]["tracking"] = rows
    rec = {**pkg.recording}
    pkgs["match-dev"] = build_package(rec, pkg.athletes, pkg.samples, [], {"note": "fictional"}, demo.at(demo.DURATION_S + 600), None, None, pkg.context, pkg.performance, pkg.baselines, pkg.roster, [])
    for name, data in pkgs.items():
        (out_dir / f"{name}.zip").write_bytes(data)
    with open(out_dir / "camera-reference.jsonl", "w") as f:
        for aid, rows in truth.items():
            for s, x, y in rows[::5]:
                f.write(json.dumps({"athlete_id": aid, "timestamp": demo.z(s), "x": x, "y": y}) + "\n")
    dev = read_package(pkgs["match-dev"])
    labels_a, labels_b = [], []
    for o in [o for o in dev.performance if o.athlete_id in ("p01", "p02") and o.kind in ("pass", "decision") and o.assessment == "assessed"][:16]:
        base = {"case_id": "match-dev", "item_id": o.id, "athlete_id": o.athlete_id, "kind": o.kind, "period_start": o.period_start.isoformat(), "period_end": o.period_end.isoformat()}
        counts = ({"attempts": o.attempts, "completions": o.completions} if o.kind == "pass" else
                  {"opportunities": o.opportunities, "rated_good": o.rated_good, "rated_acceptable": o.rated_acceptable, "rated_poor": o.rated_poor})
        labels_a.append({**base, **counts})
        other = dict(counts)
        if rng.random() < 0.3:   # fictional reviewer B sometimes counts one fewer success
            if o.kind == "pass" and other["completions"] > 0:
                other["completions"] -= 1
            elif o.kind == "decision" and other["rated_good"] > 0:
                other["rated_good"] -= 1; other["rated_poor"] += 1
        labels_b.append({**base, **other})
    (out_dir / "performance-reviewer-a.json").write_text(json.dumps(labels_a, indent=1))
    (out_dir / "performance-reviewer-b.json").write_text(json.dumps(labels_b, indent=1))
    coach_a, coach_b = [], []
    for sid, pid, t, expected, _ in demo.SCENARIOS:
        for case in ("match-dev", "match-held"):
            coach_a.append({"case_id": case, "athlete_id": pid, "at": demo.z(t), "judgment": expected})
            coach_b.append({"case_id": case, "athlete_id": pid, "at": demo.z(t), "judgment": {"S1": "keep", "S3": "keep", "S6": "keep"}.get(sid, expected)})
    (out_dir / "coach-a.json").write_text(json.dumps(coach_a, indent=1))
    (out_dir / "coach-b.json").write_text(json.dumps(coach_b, indent=1))
    ds = {"format": FORMAT, "format_version": 1, "dataset_id": "fictional-workbench-check", "fictional": True,
          "provenance": "FICTIONAL: generated by scripts/evaluate.py make-fictional from the product demo's scenario script; the held-out match uses other random draws of the same script, so it is not independent of it",
          "code_revision": None,
          "cases": [{"case_id": "match-dev", "split": "development", "package": "match-dev.zip", "label": "fictional match"},
                    {"case_id": "match-held", "split": "held_out", "package": "match-held.zip", "label": "fictional match, other draws"}],
          "references": {"camera_positions": [{"case_id": "match-dev", "file": "camera-reference.jsonl", "reviewer_id": "fictional-surveyor", "source": "FICTIONAL generated truth"}],
                         "performance_labels": [{"file": "performance-reviewer-a.json", "reviewer_id": "fictional-reviewer-a", "source": "FICTIONAL"},
                                                {"file": "performance-reviewer-b.json", "reviewer_id": "fictional-reviewer-b", "source": "FICTIONAL"}],
                         "coach_judgments": [{"file": "coach-a.json", "reviewer_id": "fictional-coach-a", "source": "FICTIONAL (written to match the demo's expected outputs)"},
                                             {"file": "coach-b.json", "reviewer_id": "fictional-coach-b", "source": "FICTIONAL (keeps three players coach a would not)"}]},
          "tuning_log": []}
    (out_dir / "dataset.json").write_text(json.dumps(ds, indent=2))
    out(f"wrote a FICTIONAL evaluation dataset to {out_dir} (dataset.json, two packages, references from fictional reviewers)")
    return 0


def main(argv=None, out=print) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    m = sub.add_parser("make-fictional", help="write a FICTIONAL dataset that exercises every question")
    m.add_argument("--out", type=Path, required=True)
    r = sub.add_parser("run", help="evaluate a dataset")
    r.add_argument("dataset", type=Path)
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--split", choices=["development", "held_out", "all"], default="development")
    r.add_argument("--index-profile", action="append", help="give twice to compare two index profiles")
    r.add_argument("--substitution-profile")
    r.add_argument("--rubric")
    r.add_argument("--match-window-s", type=float, default=0.1)
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    if args.out.exists():
        out(f"{args.out} exists; not overwritten")
        return 2
    if args.command == "make-fictional":
        return make_fictional(args.out, out)
    try:
        return run(args, out)
    except (ValueError, OSError, KeyError) as exc:
        out(f"evaluation failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
