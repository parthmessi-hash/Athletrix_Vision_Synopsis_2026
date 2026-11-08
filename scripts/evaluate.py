"""Evaluation workbench: compare engine outputs with INDEPENDENT reference annotations, keeping each question separate.

USAGE
    python scripts/evaluate.py make-fictional --out DIR          # a FICTIONAL dataset to exercise the workbench (labelled fictional)
    python scripts/evaluate.py manifest DATASET.json --out MANIFEST.json [profile options]   # pin what a run will use
    python scripts/evaluate.py pilot-pin --profile PROFILE.json --analysis-split participant|session [--held-out P3 ...] --out PIN.json
    python scripts/evaluate.py pilot (--packages A.zip [B.zip ...] | --rows ROWS.json) --out DIR [--manifest STUDY_MANIFEST.json] [--pin PIN.json]
                               # the fatigue pilot's paired report: saved pre-sprint scores vs percent increase in sprint time
    python scripts/evaluate.py run DATASET.json --out DIR [--split development|held_out|all] [--manifest MANIFEST.json]
                               [--index-profile A.json [--index-profile B.json]] [--substitution-profile S.json] [--rubric R.json]
                               [--expected EXPECTED.json] [--no-sensitivity]

DATASET (format "athletrix-evaluation-dataset", version 1; paths relative to the dataset file):
    dataset_id, fictional (true for made-up data), provenance, code_revision (as supplied),
    cases: [{case_id, split: "development" | "held_out", package: PATH (a recording package), label}],
    references: {camera_positions: [{case_id, file, reviewer_id, source}],   JSON Lines {athlete_id, timestamp, x, y}
                 performance_labels: [{file, reviewer_id, source}],          JSON list {case_id, item_id, athlete_id, kind, period_start, period_end, ...counts or outcome}
                 coach_judgments: [{file, reviewer_id, source}]},            JSON list {case_id, athlete_id, at, judgment}
    tuning_log: [{date, what, split_used}]                                   every choice made while looking at results

QUESTIONS (reported separately; agreement on one says nothing about another). The report is ordered as a pilot rehearsal:
  Q1 software      did the software record and reproduce the inputs? Imported counts equal the package's, a re-export
                   carries the same samples, a review saved during the run reproduces, and a deliberately altered and a
                   deliberately removed sample are both detected by reproduction.
  Q2 camera        how accurately did the camera measure movement (below) and preserve identity: a matched engine sample
                   that lies closer to ANOTHER athlete's reference position than to its own (by at least --swap-margin-m)
                   is reported as a possible identity swap.
  Q3 reviewers     how much did independent reviewers agree on performance evidence: the label files below and, when
                   given, blind annotation sheets (references.annotation_sheets, app/core/annotation.py).
  Q4 recommendation  did the output agree with a reviewer's judgment, under five variants on identical rows:
                   descriptive (no recommendation, availability only), score_only, context_aware, context_aware with the
                   athlete's physiology stream removed over the review lookback (stream unavailable), and context_aware with
                   the athlete's performance evidence marked identity-uncertain.
SELF-CHECK (--expected): independently written expected outcomes for key rows are compared with context_aware; a deliberately
wrong expectation and a deliberately broken policy must both be caught, or the run says the check is not sensitive.
SENSITIVITY (development split only, never held out): each fictional substitution setting moved one step at a time, with
the number of rows whose output changes. It shows what matters in the made-up settings; it chooses nothing.
MANIFEST: recording ids, source types, calibration and time-mapping metadata, profile hashes, code revision, splits and
comparison rules. With --manifest, a run refuses packages or profiles whose hashes differ from the pinned ones.

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



# ---- Q1 software, Q2 identity, Q3 annotation sheets, Q4 variants, self-check, sensitivity, manifest --------------------------

def software(doc: dict, cases: Cases) -> dict:
    """Recording and reproduction checks on the isolated evaluation database (run last: it alters and restores samples)."""
    import sqlite3  # noqa: PLC0415

    from app.core.recording_package import read_package  # noqa: PLC0415
    rows = []
    for case, rid in cases.rec.items():
        pkg = cases.packages[case]["pkg"]
        detail = cases.get(f"/recordings/{rid}")
        counts_ok = all(a["physiological"]["sample_count"] == len(pkg.samples.get(a["athlete"]["id"], {}).get("physiological", []))
                        and a["tracking"]["sample_count"] == len(pkg.samples.get(a["athlete"]["id"], {}).get("tracking", [])) for a in detail["recorded"])
        r = cases.engine.client.get(f"/api/v1/recordings/{rid}/package.zip")
        again = read_package(r.content)
        same = all([s.model_dump_json() for s in again.samples.get(aid, {}).get(st, [])] == [s.model_dump_json() for s in pkg.samples.get(aid, {}).get(st, [])]
                   for aid in pkg.samples for st in ("physiological", "tracking"))
        strap = next((aid for aid in pkg.samples if pkg.samples[aid].get("physiological")), None)
        row = {"case_id": case, "imported_counts_match_package": counts_ok, "re_export_carries_the_same_samples": same, "package_format": pkg.manifest["format_version"]}
        if strap:
            at = pkg.recording["stopped_at"]
            saved = cases.engine.post(f"/recordings/{rid}/athletes/{strap}/reviews", {"kind": "physiological_index", "at": at, "label": "evaluation software check"})
            rep = lambda: cases.get(f"/recordings/{rid}/reviews/{saved['id']}/reproduce")["status"]  # noqa: E731
            row["saved_review_reproduces"] = rep() == "reproduced"
            con = sqlite3.connect(cases.engine.db.path)
            try:
                key = con.execute("SELECT ts_utc, payload FROM recording_physiological_samples WHERE recording_id = ? AND athlete_id = ? ORDER BY ts_utc DESC LIMIT 1", (rid, strap)).fetchone()
                d = json.loads(key[1]); d["heart_rate_bpm"] = (d.get("heart_rate_bpm") or 100) + 1
                con.execute("UPDATE recording_physiological_samples SET payload = ? WHERE recording_id = ? AND athlete_id = ? AND ts_utc = ?", (json.dumps(d), rid, strap, key[0])); con.commit()
                row["altered_sample_detected"] = rep() == "evidence_changed"
                con.execute("UPDATE recording_physiological_samples SET payload = ? WHERE recording_id = ? AND athlete_id = ? AND ts_utc = ?", (key[1], rid, strap, key[0])); con.commit()
                row["restored_reproduces"] = rep() == "reproduced"
                full = con.execute("SELECT * FROM recording_physiological_samples WHERE recording_id = ? AND athlete_id = ? AND ts_utc = ?", (rid, strap, key[0])).fetchone()
                con.execute("DELETE FROM recording_physiological_samples WHERE recording_id = ? AND athlete_id = ? AND ts_utc = ?", (rid, strap, key[0])); con.commit()
                row["removed_sample_detected"] = rep() == "evidence_changed"
                cols = [c[1] for c in con.execute("PRAGMA table_info(recording_physiological_samples)")]
                con.execute(f"INSERT INTO recording_physiological_samples ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", full); con.commit()
            finally:
                con.close()
        rows.append(row)
    ok = all(v for r in rows for k, v in r.items() if isinstance(v, bool))
    return {"cases": rows, "passed": ok, "note": "checks on this software and these packages only; they say nothing about whether the inputs were measured correctly"}


def identity(doc: dict, cases: Cases, window_s: float, margin_m: float) -> dict:
    out = []
    for ref in doc["references"].get("camera_positions", []):
        if ref["case_id"] not in cases.packages:
            continue
        pkg = cases.packages[ref["case_id"]]["pkg"]
        rows = [json.loads(line) for line in (doc["_base"] / ref["file"]).read_text().splitlines() if line.strip()]
        by_t: dict[str, dict[str, tuple]] = {}
        for r in rows:
            by_t.setdefault(r["timestamp"], {})[r["athlete_id"]] = (r["x"], r["y"])
        athletes = sorted({r["athlete_id"] for r in rows})
        checked, swaps, segs = 0, [], {}
        for aid in athletes:
            for s in pkg.samples.get(aid, {}).get("tracking", []):
                segs.setdefault(aid, set()).add(s.segment_id)
                stamp = min(by_t, key=lambda k: abs((parse_t(k) - s.timestamp).total_seconds()))
                if abs((parse_t(stamp) - s.timestamp).total_seconds()) > window_s or aid not in by_t[stamp] or len(by_t[stamp]) < 2:
                    continue
                checked += 1
                own = ((s.x_position - by_t[stamp][aid][0]) ** 2 + (s.y_position - by_t[stamp][aid][1]) ** 2) ** 0.5
                other = min((((s.x_position - x) ** 2 + (s.y_position - y) ** 2) ** 0.5, o) for o, (x, y) in by_t[stamp].items() if o != aid)
                if other[0] + margin_m < own:
                    swaps.append({"athlete_id": aid, "timestamp": s.timestamp.isoformat(), "closer_to": other[1], "segment_id": s.segment_id})
        out.append({"case_id": ref["case_id"], "samples_checked": checked, "possible_swaps": len(swaps), "swap_segments": sorted({(w["athlete_id"], w["segment_id"]) for w in swaps}),
                    "first_swaps": swaps[:5], "segments_per_athlete": {a: len(v) for a, v in segs.items()},
                    "rule": f"a sample closer to another athlete's reference than to its own by more than {margin_m} m"})
    return {"by_reference": out, "note": "possible swaps are flagged for review; identity was confirmed by whoever made the assignments (see their operator_work)"}


def annotation_sheets(doc: dict) -> dict | None:
    from app.core.annotation import SheetError, compare  # noqa: PLC0415
    refs = doc["references"].get("annotation_sheets") or []
    if len(refs) < 2:
        return None
    sheets = [json.loads((doc["_base"] / r["file"]).read_text()) for r in refs[:2]]
    try:
        return compare(*sheets)
    except SheetError as exc:
        return {"error": str(exc)}


def variant_inputs(inp, variant: str, prof):
    """A copy of the review inputs for one variant (never the stored data)."""
    from dataclasses import replace  # noqa: PLC0415
    v = replace(inp, athletes=dict(inp.athletes))
    if variant == "score_only":
        v.rule_variant = "score_only"
    elif variant == "stream_unavailable":
        d = v.athletes[inp.athlete_id]
        cut = inp.at - timedelta(seconds=prof.lookback_seconds + 3600)
        v.athletes[inp.athlete_id] = replace(d, samples=[(s, r) for s, r in d.samples if s.timestamp <= cut])
    elif variant == "identity_uncertain":
        d = v.athletes[inp.athlete_id]
        v.athletes[inp.athlete_id] = replace(d, observations=[o.model_copy(update={"identity": "uncertain"}) for o in d.observations])
    return v


VARIANTS = ("descriptive", "score_only", "context_aware", "stream_unavailable", "identity_uncertain")


def review_rows(doc: dict, cases: Cases, rows: list[dict], profile_override=None) -> list[dict]:
    from app.api import review as api  # noqa: PLC0415
    from app.core.substitution import substitution_review  # noqa: PLC0415
    from app.db import get_database  # noqa: PLC0415
    from app.store.recordings import recording_store  # noqa: PLC0415
    parsed = api.parse_docs(api.configured_docs())
    if profile_override is not None:
        parsed = {**parsed, "substitution": profile_override}
    out = []
    for j in rows:
        rid = cases.rec[j["case_id"]]
        with get_database().read():
            rec = recording_store.get(rid)
            inp, _ = api.gather(rec, j["athlete_id"], parse_t(j["at"]), parsed, "retrospective")
        row = {**j}
        prof = parsed["substitution"][0] if parsed["substitution"] else None
        for variant in VARIANTS:
            if variant == "descriptive":
                r = substitution_review(inp)
                p = r.get("player") or {}
                row["descriptive"] = {"physiological_index": (p.get("physiology") or {}).get("available"), "performance_composite": (p.get("performance") or {}).get("available")}
                continue
            row[variant] = substitution_review(variant_inputs(inp, variant, prof))["outcome"] if prof else "insufficient_evidence"
        out.append(row)
    return out


def substitution_variants(doc: dict, cases: Cases) -> dict:
    res = {"variants": list(VARIANTS), "by_reviewer": [],
           "note": "agreement with a reviewer's judgment is not physiological or performance accuracy; rows are repeated judgments of the same athletes, not independent participants"}
    for ref in doc["references"].get("coach_judgments", []):
        rows = [x for x in json.loads((doc["_base"] / ref["file"]).read_text()) if x["case_id"] in cases.rec]
        done = review_rows(doc, cases, rows)
        agree = {v: sum(r[v] == r["judgment"] for r in done) for v in VARIANTS if v != "descriptive"}
        insufficient = {v: sum(r[v] == "insufficient_evidence" for r in done) for v in VARIANTS if v != "descriptive"}
        by_split = {}
        split_of = {c["case_id"]: c["split"] for c in doc["_cases"]}
        for sp in sorted({split_of[r["case_id"]] for r in done}):
            sub = [r for r in done if split_of[r["case_id"]] == sp]
            by_split[sp] = {"rows": len(sub), **{v: sum(r[v] == r["judgment"] for r in sub) for v in VARIANTS if v != "descriptive"}}
        res["by_reviewer"].append({"reviewer_id": ref["reviewer_id"], "source": ref.get("source"), "judgments": len(done),
                                   "athletes": len({r["athlete_id"] for r in done}), "cases": len({r["case_id"] for r in done}),
                                   "agreement": agree, "insufficient_evidence_outputs": insufficient, "by_split": by_split, "rows": done})
    return res


def self_check(doc: dict, cases: Cases, expected_path: Path | None) -> dict | None:
    """Independently written expectations vs context_aware; then a wrong expectation and a broken policy must be caught."""
    if expected_path is None:
        return None
    exp = json.loads(Path(expected_path).read_text())
    rows = [{"case_id": e["case_id"], "athlete_id": e["athlete_id"], "at": e["at"], "judgment": e["expected"], "why": e.get("why")} for e in exp["expectations"] if e["case_id"] in cases.rec]
    got = review_rows(doc, cases, rows)
    mism = [{"case_id": r["case_id"], "athlete_id": r["athlete_id"], "at": r["at"], "expected": r["judgment"], "got": r["context_aware"]} for r in got if r["context_aware"] != r["judgment"]]
    other = {"keep": "consider_replacement", "consider_replacement": "keep", "review_later": "keep", "insufficient_evidence": "keep"}
    wrong = [{**r, "judgment": other[r["judgment"]]} if i == 0 else r for i, r in enumerate(rows)]
    caught_wrong = any(r["context_aware"] != r["judgment"] for r in review_rows(doc, cases, wrong[:1]))
    from app.api import review as api  # noqa: PLC0415
    sub = api.parse_docs(api.configured_docs())["substitution"]
    caught_policy = None
    if sub:
        broken = sub[0].model_copy(update={"performance": sub[0].performance.model_copy(update={"concern_below": 0.0, "clear_at": 0.0}),
                                           "physiology": sub[0].physiology.model_copy(update={"concern_at": 100.0, "clear_below": 100.0})})
        bad = review_rows(doc, cases, rows, profile_override=(broken, "deliberately-broken"))
        caught_policy = any(b["context_aware"] != r["judgment"] for b, r in zip(bad, rows))
    return {"file": Path(expected_path).name, "source": exp.get("source"), "rows": len(rows), "matches": len(rows) - len(mism), "mismatches": mism,
            "deliberately_wrong_expectation_caught": caught_wrong, "deliberately_broken_policy_caught": caught_policy,
            "note": "expectations written by hand from the scenario descriptions, not from this code's output; a check that cannot catch a planted error is reported as not sensitive"}


def sensitivity(doc: dict, cases: Cases) -> dict | None:
    from app.api import review as api  # noqa: PLC0415
    dev = {c["case_id"] for c in doc["_cases"] if c["split"] == "development"}
    refs = doc["references"].get("coach_judgments", [])
    sub = api.parse_docs(api.configured_docs())["substitution"]
    if not dev or not refs or not sub:
        return None
    rows = [x for x in json.loads((doc["_base"] / refs[0]["file"]).read_text()) if x["case_id"] in dev]
    base = {(r["case_id"], r["athlete_id"], r["at"]): r["context_aware"] for r in review_rows(doc, cases, rows)}
    p = sub[0]
    steps = [("physiology.concern_at", -10), ("physiology.concern_at", 10), ("physiology.persistence_points", -1), ("physiology.persistence_points", 1),
             ("performance.concern_below", -10), ("performance.concern_below", 10), ("performance.persistence_points", -1), ("performance.persistence_points", 1),
             ("cooldown_seconds", "x0"), ("cooldown_seconds", "x2")]
    out = []
    for path, step in steps:
        part, _, field = path.partition(".")
        if field:
            sec = getattr(p, part)
            v = getattr(sec, field)
            nv = max(1, v + step) if isinstance(v, int) else min(100.0, max(0.0, v + step))
            upd = {field: nv}
            if field == "concern_at":
                upd["clear_below"] = min(sec.clear_below, nv)
            if field == "concern_below":
                upd["clear_at"] = max(sec.clear_at, nv)
            q = p.model_copy(update={part: sec.model_copy(update=upd)})
            label = f"{path} {v:g} -> {nv:g}"
        else:
            nv = 0.0 if step == "x0" else p.cooldown_seconds * 2
            q = p.model_copy(update={"cooldown_seconds": nv})
            label = f"cooldown_seconds {p.cooldown_seconds:g} -> {nv:g}"
        got = review_rows(doc, cases, rows, profile_override=(q, "sensitivity"))
        changed = [{"case_id": r["case_id"], "athlete_id": r["athlete_id"], "at": r["at"], "from": base[(r["case_id"], r["athlete_id"], r["at"])], "to": r["context_aware"]}
                   for r in got if r["context_aware"] != base[(r["case_id"], r["athlete_id"], r["at"])]]
        out.append({"setting": label, "rows_changed": len(changed), "of": len(rows), "changes": changed})
    return {"split": "development", "reviewer_rows_from": refs[0]["reviewer_id"], "steps": out,
            "note": "FICTIONAL settings moved one step at a time on the development split only. Shows which made-up settings change outputs; it selects no threshold and was not run on held-out cases."}


def build_manifest(doc: dict, base: dict, args) -> dict:
    from app.core.recording_package import read_package  # noqa: PLC0415
    cases = []
    for c in doc["_cases"]:
        data = (doc["_base"] / c["package"]).read_bytes()
        pkg = read_package(data)
        sources = sorted({str(s.source) for a in pkg.samples.values() for st in a.values() for s in st if getattr(s, "source", None)})
        op = (pkg.configuration.operator_supplied.metadata if pkg.configuration and pkg.configuration.operator_supplied else {}) or {}
        cases.append({"case_id": c["case_id"], "split": c["split"], "package": c["package"], "package_sha256": hashlib.sha256(data).hexdigest(),
                      "recording_id": pkg.recording["id"], "source_types": sources,
                      "calibration_sha256": op.get("calibration_sha256"), "time_mapping": op.get("time_mapping") or "not recorded in the package (timelines treated as independent)"})
    try:
        rev = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=5).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        rev = None
    return {"format": "athletrix-evaluation-manifest", "format_version": 1, "dataset_id": doc["dataset_id"], "fictional": bool(doc.get("fictional")), "code_revision": rev,
            "cases": cases, "profiles": {k: {"file": v.name, "sha256": hashlib.sha256(v.read_bytes()).hexdigest()} for k, v in base.items()},
            "references": {k: [{**r, "sha256": hashlib.sha256((doc["_base"] / r["file"]).read_bytes()).hexdigest()} for r in v] for k, v in doc["references"].items()},
            "comparison_rules": {"camera_match_window_s": args.match_window_s, "swap_margin_m": args.swap_margin_m, "performance_match": "same athlete and kind, overlap >= half of the shorter period",
                                 "recommendation_variants": list(VARIANTS), "knowledge": "retrospective"}}


def check_manifest(pinned: dict, now: dict) -> list[str]:
    problems = []
    a = {c["case_id"]: c["package_sha256"] for c in pinned["cases"]}
    for c in now["cases"]:
        if c["case_id"] in a and a[c["case_id"]] != c["package_sha256"]:
            problems.append(f"case {c['case_id']}: package differs from the manifest")
    for k, v in pinned["profiles"].items():
        if now["profiles"].get(k, {}).get("sha256") != v["sha256"]:
            problems.append(f"{k}: profile differs from the manifest")
    return problems


# ---- run ----------------------------------------------------------------------------------------------------------------------

def run(args, out) -> int:
    doc = load_dataset(args.dataset, args.split)
    base = {"PHYSIOLOGICAL_INDEX_PROFILE_PATH": Path(args.index_profile[0]) if args.index_profile else demo.PROFILE_FILES["PHYSIOLOGICAL_INDEX_PROFILE_PATH"],
            "SUBSTITUTION_PROFILE_PATH": Path(args.substitution_profile) if args.substitution_profile else demo.PROFILE_FILES["SUBSTITUTION_PROFILE_PATH"],
            "PERFORMANCE_RUBRIC_PATH": Path(args.rubric) if args.rubric else demo.PROFILE_FILES["PERFORMANCE_RUBRIC_PATH"]}
    saved_env = {k: os.environ.get(k) for k in base}
    manifest = build_manifest(doc, base, args)
    if args.manifest:
        problems = check_manifest(json.loads(Path(args.manifest).read_text()), manifest)
        if problems:
            out("refused: " + "; ".join(problems))
            return 1
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
                   "manifest": manifest,
                   "camera": camera(doc, cases, args.match_window_s), "identity": identity(doc, cases, args.match_window_s, args.swap_margin_m),
                   "performance": performance(doc, cases), "annotation_sheets": annotation_sheets(doc),
                   "profiles": profiles(doc, cases, [Path(p) for p in args.index_profile or []], base), "substitution": substitution(doc, cases),
                   "recommendation_variants": substitution_variants(doc, cases), "self_check": self_check(doc, cases, args.expected),
                   "sensitivity": None if args.no_sensitivity or args.split == "held_out" else sensitivity(doc, cases)}
        results["software"] = software(doc, cases)   # last: it alters and restores samples in the isolated database
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
         "Questions are kept apart: agreement on one says nothing about another. Rows, frames and repeated windows of the same athletes are not independent participants.", "",
         "## Q1. Did the software record and reproduce the inputs?"]
    sw = r.get("software") or {}
    for c in sw.get("cases", []):
        L.append(f"- {c['case_id']}: " + ", ".join(f"{k.replace('_', ' ')} {'yes' if v else 'NO'}" for k, v in c.items() if isinstance(v, bool)))
    L += [f"- overall: {'passed' if sw.get('passed') else 'FAILED'}. {sw.get('note', '')}", "", "## Q2. Camera movement and identity", "### Positions", r["camera"]["rule"], ""]
    for c in r["camera"]["by_reference"]:
        e = c["position_error_m"] or {}
        L.append(f"- {c['case_id']}, reviewer {c['reviewer_id']} ({c['source']}): {c['matched']} of {c['reference_points']} reference points matched (coverage {c['coverage']}), {c['unmatched_coverage_gaps']} coverage gaps; "
                 f"position error median {e.get('median')} m, p95 {e.get('p95')} m, max {e.get('max')} m. {c['note']}.")
    L += ["", "### Identity"]
    for c in (r.get("identity") or {}).get("by_reference", []):
        L.append(f"- {c['case_id']}: {c['samples_checked']} samples checked against simultaneous references, {c['possible_swaps']} possible swaps in segments {c['swap_segments']} ({c['rule']}); segments per athlete {c['segments_per_athlete']}")
    L += ["", "## Q3. Reviewer agreement on performance evidence", "### Engine evidence vs reference labels", r["performance"]["rule"], ""]
    for b in r["performance"]["by_reviewer"]:
        L.append(f"- reviewer {b['reviewer_id']} ({b['source']}): {b['compared']} of {b['labels']} labels compared ({b['unmatched']} unmatched), {b['exact_agreement']} exact agreements; rate difference {b['rate_difference']}")
    L.append(f"- {r['performance']['items_with_several_reviewers']} items labelled by several reviewers; {len(r['performance']['disagreements_between_reviewers'])} disagreements kept as they are:")
    for d in r["performance"]["disagreements_between_reviewers"]:
        L.append(f"  - {d['case_id']} {d['item_id']}: " + "; ".join(f"{k}: {json.dumps(v)}" for k, v in d["labels"].items()))
    a = r.get("annotation_sheets")
    L += ["", "### Blind annotation sheets"]
    if not a:
        L.append("No pair of annotation sheets in the dataset.")
    elif a.get("error"):
        L.append(f"Not compared: {a['error']}")
    else:
        L.append(f"Reviewers {a['reviewers'][0]} and {a['reviewers'][1]}" + (" (FICTIONAL)" if a["fictional"] else "") + ". " + a["note"])
        for q, v in a["per_question"].items():
            L.append(f"- {q}: asked {v['moments_asked']}, both assessed {v['both_assessed']}, same assessability {v['same_assessability']}, agreed {v['agreed_where_both_assessed']}")
        for d in a["disagreements"]:
            L.append(f"  - disagree {d['moment']} {d['question']}: A {json.dumps(d['a'])} | B {json.dumps(d['b'])}")
    L += ["", "## Q4. Recommendation vs reviewer judgment", r["recommendation_variants"]["note"], ""]
    for b in r["recommendation_variants"]["by_reviewer"]:
        L.append(f"- {b['reviewer_id']} ({b['source']}): {b['judgments']} rows on {b['athletes']} athletes in {b['cases']} cases. Agreement: " +
                 ", ".join(f"{k} {v}/{b['judgments']}" for k, v in b["agreement"].items()) + ". Insufficient-evidence outputs: " + ", ".join(f"{k} {v}" for k, v in b["insufficient_evidence_outputs"].items()))
        for sp, v in b["by_split"].items():
            L.append(f"  - {sp}: " + ", ".join(f"{k} {n}/{v['rows']}" for k, n in v.items() if k != "rows"))
    sc = r.get("self_check")
    L += ["", "## Self-check against independently written expectations"]
    if sc:
        L.append(f"- {sc['matches']} of {sc['rows']} rows match ({sc['source']}); mismatches: {json.dumps(sc['mismatches'])}")
        L.append(f"- a deliberately wrong expectation caught: {sc['deliberately_wrong_expectation_caught']}; a deliberately broken policy caught: {sc['deliberately_broken_policy_caught']}. {sc['note']}")
    else:
        L.append("Not run (give --expected).")
    se = r.get("sensitivity")
    L += ["", "## Sensitivity of the fictional settings (development split only)"]
    if se:
        L.append(se["note"])
        for st in se["steps"]:
            L.append(f"- {st['setting']}: {st['rows_changed']} of {st['of']} rows change" + (" · " + "; ".join(f"{c['athlete_id']} {c['from']}->{c['to']}" for c in st["changes"][:6]) if st["changes"] else ""))
    else:
        L.append("Not run.")
    L += ["", "## Physiological index profiles compared", json.dumps(r["profiles"], indent=1) if r["profiles"] else "Not run (give --index-profile twice).", "",
          "## Earlier comparison (context-aware and score-only only)", r["substitution"]["note"], ""]
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
            seg = f"seg-{aid}-{k // 300}"
            if aid == "p01" and 500 <= k < 520:   # a deliberately planted identity swap: p01's samples follow p02 (Q2 must flag it)
                x, seg = x + 5, "seg-p01-planted-swap"
            rows.append(TrackingSample(athlete_id=aid, timestamp=demo.at(s), x_position=round(x + rng.gauss(0, 0.3), 3), y_position=round(y + rng.gauss(0, 0.3), 3), speed_mps=1.0,
                                       acceleration_mps2=0.0, tracking_confidence=0.9, source="camera", device_id="fictional-cam", segment_id=seg, value_basis="estimated"))
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
    import shutil  # noqa: PLC0415
    for name in ("fictional_reviewer_a.json", "fictional_reviewer_b.json"):
        shutil.copy(ROOT / "scripts" / "examples" / "annotation" / name, out_dir / name)
    shutil.copy(ROOT / "scripts" / "examples" / "evaluation" / "fictional_expected_outcomes.json", out_dir / "expected-outcomes.json")
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
                                             {"file": "coach-b.json", "reviewer_id": "fictional-coach-b", "source": "FICTIONAL (keeps three players coach a would not)"}],
                         "annotation_sheets": [{"file": "fictional_reviewer_a.json", "reviewer_id": "fictional reviewer A", "source": "FICTIONAL"},
                                               {"file": "fictional_reviewer_b.json", "reviewer_id": "fictional reviewer B", "source": "FICTIONAL"}]},
          "tuning_log": []}
    (out_dir / "dataset.json").write_text(json.dumps(ds, indent=2))
    out(f"wrote a FICTIONAL evaluation dataset to {out_dir} (dataset.json, two packages, references from fictional reviewers, annotation sheets, expected-outcomes.json)")
    return 0


# ---- the fatigue pilot (app/core/pilot_analysis.py) ------------------------------------------------------------------------

def pilot_pin(args, out) -> int:
    text = Path(args.profile).read_text()
    from app.core.fatigue import parse_profile  # noqa: PLC0415
    _, sha = parse_profile(text)
    pin = {"format": "athletrix-pilot-pin", "format_version": 1, "pinned_at": datetime.now().astimezone().isoformat(timespec="seconds"),
           "profile_sha256": sha, "profile_file": Path(args.profile).name, "split_by": args.analysis_split, "held_out": sorted(args.held_out or []),
           "analysis": {"outcome": "percent increase in sprint time relative to the participant's best valid time in that session",
                        "statistic": "Pearson r per candidate on matched rows; per participant, per session and pooled", "targets": ["combined r >= 0.5", "combined r above both single-source r"]},
           "note": "Pin the profile and the analysis before collecting the held-out data. Rows whose recording started before pinned_at are never reported as held out."}
    if args.out.exists():
        out(f"{args.out} exists; not overwritten")
        return 2
    args.out.write_text(json.dumps(pin, indent=2) + "\n")
    out(f"wrote {args.out} (profile {sha[:12]}, split by {args.analysis_split}, held out: {', '.join(pin['held_out']) or 'none'})")
    return 0


MANIFEST_FORMAT = "athletrix-study-manifest"
MANIFEST_KEYS = ("study_id", "fictional", "declared_at", "profile_sha256", "baseline_procedure", "timing", "camera", "rounds", "analysis", "approval")


def load_manifest(path: Path) -> tuple[dict, str]:
    data = Path(path).read_bytes()
    m = json.loads(data)
    if m.get("format") != MANIFEST_FORMAT or m.get("format_version") != 1:
        raise ValueError(f"{path} is not an {MANIFEST_FORMAT} version 1 file")
    missing = [k for k in MANIFEST_KEYS if k not in m]
    if missing:
        raise ValueError(f"the study manifest lacks {', '.join(missing)}")
    if m["timing"].get("policy") not in ("independent_start_reference", "confirmed_sequence"):
        raise ValueError("timing.policy must be independent_start_reference or confirmed_sequence")
    if not m["profile_sha256"]:
        raise ValueError("the study manifest names no frozen profile (profile_sha256 is empty)")
    return m, hashlib.sha256(data).hexdigest()


def pilot(args, out) -> int:
    from app.core.pilot_analysis import compare, rows_from_package  # noqa: PLC0415
    from app.core.recording_package import read_package  # noqa: PLC0415
    source, rows, starts, frozen = [], [], {}, {}
    first_outcome = None   # the earliest independent result entered (engine receipt time), across the inputs
    if args.rows:
        doc = json.loads(Path(args.rows).read_text())
        rows = [{**r, "fictional": r.get("fictional", bool(doc.get("fictional")))} for r in doc["rows"]]
        source.append({"rows": Path(args.rows).name, "sha256": hashlib.sha256(Path(args.rows).read_bytes()).hexdigest()})
    for pth in args.packages or []:
        data = Path(pth).read_bytes()
        pkg = read_package(data)
        if pkg.manifest["format_version"] < 8:
            out(f"{pth}: package format {pkg.manifest['format_version']} has no study log")
            return 1
        rr = rows_from_package(pkg)
        rows += rr
        for r in rr:
            starts[(r["participant"], r["session"])] = pkg.recording["started_at"]
        for e in pkg.study:
            if e.kind == "sprint_result" and e.created_at is not None and (first_outcome is None or e.created_at < first_outcome):
                first_outcome = e.created_at
        sess = next((e for e in pkg.study if e.kind == "session" and e.profile), None)
        if sess:
            frozen[sess.profile["sha256"]] = {"version": sess.profile["version"], "fictional": sess.profile["fictional"], "text": sess.profile["text"]}
        source.append({"package": Path(pth).name, "sha256": hashlib.sha256(data).hexdigest(), "recording_id": pkg.recording["id"]})
    fict = {bool(r.get("fictional")) for r in rows}
    if len(fict) > 1:
        out("refused: the inputs mix FICTIONAL and real sessions; report them separately")
        return 1
    shas = {r.get("profile_sha256") for r in rows if r.get("profile_sha256")}
    if len(shas) > 1:
        out(f"refused: the inputs were scored with different frozen profiles ({', '.join(sorted(x[:12] for x in shas))}); one report per protocol")
        return 1
    manifest, manifest_sha, policy, methods = None, None, "confirmed_sequence", None
    if args.manifest:
        try:
            manifest, manifest_sha = load_manifest(args.manifest)
        except (OSError, ValueError) as exc:
            out(f"refused: {exc}")
            return 1
        if shas and shas != {manifest["profile_sha256"]}:
            out(f"refused: the rows were scored with profile {next(iter(shas))[:12]}, the study manifest declares {manifest['profile_sha256'][:12]}")
            return 1
        if fict and bool(manifest["fictional"]) != next(iter(fict)):
            out("refused: the study manifest and the inputs disagree about being FICTIONAL")
            return 1
        policy = manifest["timing"]["policy"]
        methods = manifest["timing"].get("reference_methods") or None
        if first_outcome is not None:
            try:
                declared = datetime.fromisoformat(str(manifest.get("declared_at")).replace("Z", "+00:00"))
                if declared.tzinfo is None:
                    raise ValueError("declared_at has no time zone")
            except ValueError:
                declared = None
            frozen_ok = declared is not None and declared <= first_outcome
            if not frozen_ok and not manifest["fictional"]:
                out(f"refused: the study manifest's declared_at ({manifest.get('declared_at') or 'missing'}) is not before the first sprint result entered "
                    f"({first_outcome.isoformat()}): features, weights, baseline rules and analysis must be frozen before any outcome is seen")
                return 1
    pin = json.loads(Path(args.pin).read_text()) if args.pin else None
    if pin and shas and shas != {pin["profile_sha256"]}:
        out(f"refused: the rows were scored with profile(s) {sorted(x[:12] for x in shas)}, the pin names {pin['profile_sha256'][:12]}")
        return 1
    decl = manifest or pin
    held = {"status": "nothing declared held out: no held-out claim"}
    if decl and (decl.get("held_out") or (decl.get("analysis") or {}).get("held_out")):
        held_list = decl.get("held_out") or decl["analysis"]["held_out"]
        key = "participant" if (decl.get("split_by") or (decl.get("analysis") or {}).get("split_by")) == "participant" else "session"
        declared_at = datetime.fromisoformat(decl.get("pinned_at") or decl["declared_at"])
        test = [r for r in rows if r[key] in held_list]
        late = [r for r in test if (r["participant"], r["session"]) not in starts or datetime.fromisoformat(starts[(r["participant"], r["session"])].replace("Z", "+00:00")) < declared_at]
        if not test:
            held = {"status": "no held-out rows in this data"}
        elif late:
            held = {"status": "NOT HELD OUT: some of these rows were recorded before the declaration (or their recording time is unknown), so they could have influenced the choices"}
        else:
            held = {"status": f"held out by {key} ({', '.join(held_list)}), recorded after the declaration", "comparison": compare(test, policy, methods)}
        held["limits"] = (f"{len({r['participant'] for r in rows})} participants in total: a held-out {key} is one or two people, so any held-out figure describes those "
                          "few rounds only and is not evidence for other players.")
        rows = [r for r in rows if r[key] not in held_list] if test else rows
    res = compare(rows, policy, methods)
    res.update(source=source, fictional=bool(fict and next(iter(fict))), pin=pin, study_manifest=manifest, study_manifest_sha256=manifest_sha, held_out=held,
               generated_at=datetime.now().astimezone().isoformat(timespec="seconds"), frozen_profiles=frozen,
               frozen_before_first_outcome=None if not (manifest and first_outcome) else
               (datetime.fromisoformat(str(manifest.get("declared_at")).replace("Z", "+00:00")) <= first_outcome if manifest.get("declared_at") else False),
               first_outcome_entered_at=first_outcome.isoformat() if first_outcome else None,
               analysis_choices={"outcome": res["outcome"], "policy": policy, "reference_methods": methods or "all timed methods",
                                 "statistic": "Pearson r on the policy-eligible rows where all three candidates and the outcome exist",
                                 "targets": ["combined r >= 0.5", "combined r above both single-source r"], "small_sample_flag_below": 5, "tuned_on_outcomes": False})
    if args.out.exists():
        out(f"{args.out} exists; not overwritten")
        return 2
    args.out.mkdir(parents=True)
    (args.out / "results.json").write_text(json.dumps(res, indent=2, default=str))
    (args.out / "report.md").write_text(pilot_report(res))
    out(f"wrote {args.out / 'report.md'} and results.json" + (" · FICTIONAL" if res["fictional"] else ""))
    return 0


def pilot_report(r: dict) -> str:
    def rr(x):
        return (x.get("why") or "undefined") if x["r"] is None else f"{x['r']:+.3f}"
    verdict = lambda v: "MET" if v is True else ("NOT MET" if v is False else "UNDEFINED")  # noqa: E731
    L = ["# Fatigue pilot: paired report", ""]
    if r["fictional"]:
        L += ["**FICTIONAL data: made-up readings and made-up sprint times. This exercises the analysis; it shows nothing about real players or fatigue.**", ""]
    p0 = r["pooled"]
    n = r["matched_rows"]
    rc, rv, rk = (p0[c]["r"] for c in ("combined", "vest_only", "camera_only"))
    ro = p0["round_order"]
    L += ["## Result at a glance", ""]
    if n < 5 or None in (rc, rv, rk):
        L.append(f"**INCONCLUSIVE**: {n} round(s) have all three estimates and an outcome on the same eligible rounds; at least 5 are needed "
                 "for any correlation, and a few rounds from few people cannot answer the question either way.")
    else:
        better = rc > rv and rc > rk
        L.append(f"On the same {n} eligible rounds: combined r = {rc:+.3f}, wearable-only r = {rv:+.3f}, camera-only r = {rk:+.3f}. " +
                 ("**The combined estimate tracked sprint slowdown better than both single-source estimates here.**" if better else
                  "**NEGATIVE: the combined estimate did NOT track sprint slowdown better than both single-source estimates here.**"))
        L.append(f"Study target combined r >= 0.5: **{'met' if rc >= 0.5 else 'not met'}** (a target, not proof).")
        rcr = (ro.get("round_vs_combined") or {}).get("r")
        if rcr is not None and abs(rcr) >= abs(rc):
            L.append(f"Caution: the combined estimate follows round order (r = {rcr:+.3f}) at least as strongly as it follows the outcome; it may mostly track time passing.")
    if r.get("frozen_before_first_outcome") is False:
        L.append("**The study manifest was NOT declared before the first outcome was entered: these figures cannot test frozen choices.**")
    L += ["", "Sprint slowdown is the practical outcome compared against; it is not a direct measurement of fatigue.", ""]
    L += [f"Outcome: {r['outcome']}.", "",
          f"Timing policy: **{r['policy']}** ({'declared in study manifest ' + r['study_manifest_sha256'][:12] if r.get('study_manifest_sha256') else 'default: no study manifest given'}). "
          f"Reference attempts: {r['reference_methods']}, rounds not marked invalid.", "",
          f"Primary rows (policy-eligible, all three scores and the outcome): **{r['matched_rows']}** from {r['participants']} participants and {r['sessions']} "
          "participant-sessions. Rounds of one person are repeated observations, not independent participants.", "",
          "## Timing evidence of the saved predictions", "", "| Category / basis | Rounds |", "|---|---|"]
    L += [f"| {k} | {v} |" for k, v in sorted(r["timing_evidence_counts"].items())]
    L += ["", "## Best valid reference per participant and session", "", "| Participant/session | Round | Time (s) | Method | Result entry | Corrected from |", "|---|---|---|---|---|---|"]
    L += [f"| {k} | {v['round']} | {v['time_s']} | {v.get('method')} | {v.get('result_id')} | {', '.join(map(str, v['corrected_from'])) or '-'} |" for k, v in r["best_references"].items()]
    L += ["", "## Coverage per candidate", "", "| Candidate | Rows | Usable | Excluded | Reasons |", "|---|---|---|---|---|"]
    for c, v in r["coverage"].items():
        L.append(f"| {c} | {v['rows']} | {v['usable']} | {v['excluded']} | " + "; ".join(f"{k} ({n})" for k, n in v["excluded_reasons"].items()) + " |")
    p, t = r["pooled"], r["targets"]
    L += ["", "## Primary comparison (pooled; repeated rounds from few people)", "", "| Candidate | r | n | flag |", "|---|---|---|---|"]
    for c in ("vest_only", "camera_only", "combined"):
        L.append(f"| {c} | {rr(p[c])} | {p[c]['n']} | {p[c].get('flag') or p[c].get('why') or ''} |")
    L += ["", f"Combined minus vest-only: {p['combined_minus_vest_only']}; combined minus camera-only: {p['combined_minus_camera_only']}.", "",
          f"**Engineering targets** (goals, not proof of effectiveness): combined r >= 0.5: **{verdict(t['combined_r_at_least_0.5'])}**; "
          f"combined above both single-source: **{verdict(t['combined_above_both_single_source'])}**.", "",
          f"Round order: r(round, time increase) = {rr(p['round_order']['round_vs_time_increase'])}; " +
          ", ".join(f"r(round, {c}) = {rr(p['round_order'][f'round_vs_{c}'])}" for c in ("vest_only", "camera_only", "combined")) +
          ". If everything rises with the round number, a score can correlate with the outcome simply because both grow with time.", "",
          "## By participant", "", "| Participant | rows | vest-only r | camera-only r | combined r | combined wins? | flags |", "|---|---|---|---|---|---|---|"]
    for k, b in r["by_participant"].items():
        wins = None if None in (b["combined"]["r"], b["vest_only"]["r"], b["camera_only"]["r"]) else b["combined"]["r"] > max(b["vest_only"]["r"], b["camera_only"]["r"])
        L.append(f"| {k} | {b['rows']} | {rr(b['vest_only'])} | {rr(b['camera_only'])} | {rr(b['combined'])} | {'yes' if wins else 'NO' if wins is False else 'undefined'} | " +
                 "; ".join(sorted({x.get("flag") or x.get("why") for x in (b["vest_only"], b["camera_only"], b["combined"]) if x.get("flag") or x.get("why")})) + " |")
    L += ["", "## By participant and session", "", "| Participant/session | rows | vest-only r | camera-only r | combined r |", "|---|---|---|---|---|"]
    L += [f"| {k} | {b['rows']} | {rr(b['vest_only'])} | {rr(b['camera_only'])} | {rr(b['combined'])} |" for k, b in r["by_session"].items()]
    e = r["exploratory_including_unverified_timing"]
    L += ["", "## Exploratory: including rounds whose save-before-start order is unverified (never the primary result)", "",
          f"{e['rows']} rows: " + ", ".join(f"{c} {rr(e[c])}" for c in ("vest_only", "camera_only", "combined")), "",
          "## Rounds left out of the primary comparison", ""] + ([f"- {x['participant']} {x['session']} round {x['round']}: {x['reason']}" for x in r["ineligible"]] or ["- none"])
    part = [x for x in r["rows"] if x.get("started_without_usable_data")]
    opened = [x for x in r["rows"] if x.get("opens_camera_reference")]
    L += ["", "## Rounds started without usable data (operator's reason; the team's valid-round rule decides)", ""] + \
         ([f"- {x['participant']} {x['session']} round {x['round']}: {x['started_without_usable_data']}" for x in part] or ["- none"])
    L += ["", "Camera reference opened at: " + (", ".join(f"{x['participant']} {x['session']} round {x['round']}" for x in opened) or
                                               "not recorded (recordings before 2026-11-08 open it at each athlete's first round start)")]
    L += ["", "## Which methods could score each round", "", "| Participant | Session | Round | eligible | combined | wearable-only | camera-only | outcome |", "|---|---|---|---|---|---|---|---|"]
    inel = {(x["participant"], x["session"], x["round"]): x["reason"] for x in r["ineligible"]}
    for x in r["rows"]:
        cell = lambda c: "scored" if x["scores"][c] is not None else ("no: " + ((x.get("reasons") or {}).get(c) or ["no saved prediction"])[0][:60])  # noqa: E731
        key = (x["participant"], x["session"], x["round"])
        L.append(f"| {x['participant']} | {x['session']} | {x['round']} | {'yes' if key not in inel else 'no: ' + inel[key][:60]} | {cell('combined')} | {cell('vest_only')} | "
                 f"{cell('camera_only')} | {x['time_increase_pct'] if x['time_increase_pct'] is not None else 'missing: ' + str(x.get('outcome_missing'))} |")
    L += ["", "## Held out", r["held_out"]["status"], r["held_out"].get("limits", ""), "",
          "## Self-rating (exploratory, never a score input)", f"r(self-rating, time increase) = {rr(r['self_rating_exploratory']['self_rating_vs_time_increase'])} on {r['self_rating_exploratory']['rows']} rows.", "",
          "## Rows (trace)", "", "| Participant | Session | Round | vest | camera | combined | time (s) | increase % | timing | prediction | result (history) |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for x in r["rows"]:
        s = x["scores"]
        hist = f"{x.get('result_id') or '-'}" + (f" (was {', '.join(str(h.get('time_s') or h.get('missing_reason')) for h in x.get('result_history', []))})" if x.get("result_history") else "")
        L.append(f"| {x['participant']} | {x['session']} | {x['round']} | {s['vest_only']} | {s['camera_only']} | {s['combined']} | {x.get('time_s') or x.get('outcome_missing')} | "
                 f"{x['time_increase_pct']} | {x['timing']['category']} / {x['timing']['basis']}{' (INVALID round)' if x.get('invalid') else ''} | {x.get('prediction_id') or '-'} | {hist} |")
    L += ["", "Inputs: " + "; ".join(json.dumps(s) for s in r["source"]), f"Frozen profiles: {', '.join(k[:12] + ' ' + v['version'] for k, v in r['frozen_profiles'].items()) or 'from the rows'}"]
    return "\n".join(L) + "\n"


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
    r.add_argument("--swap-margin-m", type=float, default=1.0)
    r.add_argument("--expected", type=Path, help="independently written expected outcomes (self-check)")
    r.add_argument("--manifest", type=Path, help="refuse packages or profiles that differ from this manifest")
    r.add_argument("--no-sensitivity", action="store_true")
    mf = sub.add_parser("manifest", help="pin the cases, sources, profile hashes, code revision and comparison rules of a run")
    mf.add_argument("dataset", type=Path)
    mf.add_argument("--out", type=Path, required=True)
    mf.add_argument("--split", choices=["development", "held_out", "all"], default="all")
    mf.add_argument("--index-profile", action="append")
    mf.add_argument("--substitution-profile")
    mf.add_argument("--rubric")
    mf.add_argument("--match-window-s", type=float, default=0.1)
    mf.add_argument("--swap-margin-m", type=float, default=1.0)
    pp = sub.add_parser("pilot", help="the fatigue pilot's paired report")
    g = pp.add_mutually_exclusive_group(required=True)
    g.add_argument("--packages", nargs="+")
    g.add_argument("--rows", type=Path)
    pp.add_argument("--out", type=Path, required=True)
    pp.add_argument("--pin", type=Path)
    pp.add_argument("--manifest", type=Path, help="the study manifest declared before outcomes were collected (scripts/examples/pilot/study_manifest_template.json)")
    pn = sub.add_parser("pilot-pin", help="pin the pilot profile and analysis before collecting held-out data")
    pn.add_argument("--profile", type=Path, required=True)
    pn.add_argument("--analysis-split", choices=["participant", "session"], required=True)
    pn.add_argument("--held-out", nargs="*")
    pn.add_argument("--out", type=Path, required=True)
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    if args.command == "pilot":
        return pilot(args, out)
    if args.command == "pilot-pin":
        return pilot_pin(args, out)
    if args.out.exists():
        out(f"{args.out} exists; not overwritten")
        return 2
    if args.command == "make-fictional":
        return make_fictional(args.out, out)
    if args.command == "manifest":
        doc = load_dataset(args.dataset, args.split)
        base = {"PHYSIOLOGICAL_INDEX_PROFILE_PATH": Path(args.index_profile[0]) if args.index_profile else demo.PROFILE_FILES["PHYSIOLOGICAL_INDEX_PROFILE_PATH"],
                "SUBSTITUTION_PROFILE_PATH": Path(args.substitution_profile) if args.substitution_profile else demo.PROFILE_FILES["SUBSTITUTION_PROFILE_PATH"],
                "PERFORMANCE_RUBRIC_PATH": Path(args.rubric) if args.rubric else demo.PROFILE_FILES["PERFORMANCE_RUBRIC_PATH"]}
        args.out.write_text(json.dumps(build_manifest(doc, base, args), indent=2) + "\n")
        out(f"wrote {args.out}")
        return 0
    try:
        return run(args, out)
    except (ValueError, OSError, KeyError) as exc:
        out(f"evaluation failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
