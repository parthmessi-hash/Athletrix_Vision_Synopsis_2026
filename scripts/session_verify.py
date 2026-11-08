"""Post-session verification of a fatigue-pilot recording export (after Stop). Reads; never changes the recording.

USAGE
    .venv/bin/python scripts/session_verify.py --package EXPORT.zip --out DIR [--manifest MANIFEST.json]
                     [--expect-rounds P1:3,P2:3] [--end-margin-s 10]
    .venv/bin/python scripts/session_verify.py --resume DIR          # rerun only the steps that failed or were not reached

WHAT IT TELLS THE TEAM (each step saves its own file in DIR, so a later failure keeps the earlier results)
  1. saved.json      the export copied byte for byte into DIR/export/ (SHA-256 checked against the original, which is
                     not touched): put DIR somewhere that survives the demo launcher's cleanup.
  2. summary.json    which recording and frozen profile were exported: recording id, start and stop, session label,
                     protocol, FICTIONAL flag, profile version and SHA-256, rules versions of the saved predictions.
  3. evidence.json   per participant and stream: sample counts and the last sample's time against Stop. A last sample
                     long before Stop (more than --end-margin-s) is flagged: readings that arrive after Stop are NOT
                     stored in the recording (measured 2026-11-09), so a short tail can mean data was still on its way.
  4. rounds.json     every round seen in the study log, with what it has: round start (and any partial-start reason),
                     saved prediction, sprint start, independent result (time, missing reason or invalid), round end,
                     timing evidence and whether it is eligible for the primary comparison, or why not. Rounds that
                     were started but never reached a prediction or result are listed as INCOMPLETE, never dropped.
                     --expect-rounds names how many rounds each participant code should have.
  5. reproduce.json  the export imported into a fresh temporary database and every saved prediction reproduced with
                     this code (deliberately unavailable scores stay unavailable; that is not a failure).
  6. report.json     the paired report (scripts/evaluate.py pilot) written to DIR/report.
  verdict.json       COMPLETE only when every expected round has a prediction and an independent result or a stated
                     reason, the evidence reaches Stop, every prediction reproduces and the report was written;
                     otherwise INCOMPLETE with each reason. Nothing is reopened, filled in or re-timed.

EXIT CODES: 0 COMPLETE · 1 INCOMPLETE (reasons listed) · 2 a step failed (earlier files kept; rerun with --resume DIR)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

STEPS = ("saved", "summary", "evidence", "rounds", "reproduce", "report")


def write(path: Path, doc: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=1, default=str))
    tmp.replace(path)


def iso(t) -> str | None:
    return None if t is None else t.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse(t: str) -> datetime:
    return datetime.fromisoformat(t.replace("Z", "+00:00"))


def load(cfg: dict):
    from app.core.recording_package import read_package  # noqa: PLC0415
    return read_package(Path(cfg["saved_package"]).read_bytes())


def step_saved(cfg: dict) -> dict:
    src = Path(cfg["package"])
    data = src.read_bytes()
    dest = Path(cfg["out"]) / "export" / src.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.read_bytes() != data:
        raise RuntimeError(f"{dest} already exists with different content; choose another --out")
    dest.write_bytes(data)
    a, b = hashlib.sha256(data).hexdigest(), hashlib.sha256(dest.read_bytes()).hexdigest()
    if a != b:
        raise RuntimeError("the copy does not match the original export")
    cfg["saved_package"] = str(dest)
    return {"original": str(src), "saved_copy": str(dest), "sha256": a, "bytes": len(data),
            "note": "the original export is not modified; keep this folder (it is outside any launcher temporary folder)"}


def step_summary(cfg: dict) -> dict:
    from app.store.study import current  # noqa: PLC0415
    pkg = load(cfg)
    cur = current(pkg.study)
    sess = next((e for e in cur if e.kind == "session"), None)
    preds = [e for e in cur if e.kind == "prediction"]
    return {"recording": {k: pkg.recording.get(k) for k in ("id", "name", "status", "started_at", "stopped_at")},
            "package_format": pkg.manifest.get("format_version"),
            "session": None if sess is None else {"label": sess.session_label, "protocol": sess.protocol, "fictional": sess.fictional,
                                                  "profile": None if not sess.profile else {k: sess.profile.get(k) for k in ("version", "sha256", "fictional")},
                                                  "clock_references": [c.model_dump(mode="json") for c in (sess.clock_references or [])]},
            "predictions": len(preds), "rules_versions": dict(Counter(str((e.prediction or {}).get("rules_version", 1)) for e in preds)),
            "study_entries": len(pkg.study)}


def step_evidence(cfg: dict) -> dict:
    pkg = load(cfg)
    stop = parse(pkg.recording["stopped_at"]) if pkg.recording.get("stopped_at") else None
    out, flags = {}, []
    for aid, streams in sorted(pkg.samples.items()):
        for stream, models in sorted(streams.items()):
            if not models:
                continue
            last = max(m.timestamp for m in models)
            tail = (stop - last).total_seconds() if stop else None
            out.setdefault(aid, {})[stream] = {"samples": len(models), "first": iso(min(m.timestamp for m in models)), "last": iso(last),
                                               "last_before_stop_s": None if tail is None else round(tail, 1)}
    wearer_ids = {e.athlete_id for e in pkg.study if e.kind == "wearer" and e.athlete_id}
    for aid in sorted(wearer_ids):
        for stream in ("physiological", "tracking"):
            x = out.get(aid, {}).get(stream)
            if x is None:
                flags.append(f"{aid}: no {stream} samples in the recording")
    # the last active wearer's streams should reach Stop
    from app.store.study import current  # noqa: PLC0415
    ws = [e for e in current(pkg.study) if e.kind == "wearer"]
    last_wearer = ws[-1].athlete_id if ws and not ws[-1].end else None
    if last_wearer:
        for stream, x in out.get(last_wearer, {}).items():
            if x["last_before_stop_s"] is not None and x["last_before_stop_s"] > cfg["end_margin_s"]:
                flags.append(f"{last_wearer} {stream}: last sample {x['last_before_stop_s']} s before Stop (more than {cfg['end_margin_s']:g} s): "
                             "readings still on their way at Stop are not stored in the recording")
    return {"stop": iso(stop), "by_athlete": out, "active_wearer_at_stop": last_wearer, "flags": flags}


def step_rounds(cfg: dict) -> dict:
    from app.core.pilot_analysis import compare, rows_from_package  # noqa: PLC0415
    from app.store.study import current  # noqa: PLC0415
    pkg = load(cfg)
    cur = current(pkg.study)
    code = {e.athlete_id: e.participant_code for e in cur if e.kind == "wearer" and e.participant_code}
    policy = "confirmed_sequence"
    methods = None
    if cfg.get("manifest"):
        man = json.loads(Path(cfg["manifest"]).read_text())
        policy = (man.get("timing") or {}).get("policy") or policy
        methods = (man.get("analysis") or {}).get("reference_methods")
    rows = rows_from_package(pkg)
    comp = compare(rows, policy=policy, reference_methods=methods) if rows else {"rows": [], "ineligible": []}
    ineligible = {(x["participant"], x["round"]): x["reason"] for x in comp["ineligible"]}
    by_row = {(r["participant"], r["round"]): r for r in comp["rows"]}
    keys = sorted({(e.athlete_id, e.round) for e in cur if e.round is not None and e.athlete_id})
    rounds, problems = [], []
    for aid, rnd in keys:
        mine = [e for e in cur if (e.athlete_id, e.round) == (aid, rnd)]
        kinds = {e.kind for e in mine}
        pc = code.get(aid, aid)
        start = next((e for e in mine if e.kind == "round_start"), None)
        end = next((e for e in mine if e.kind == "round_end"), None)
        res = next((e for e in reversed(mine) if e.kind == "sprint_result"), None)
        r = by_row.get((pc, rnd))
        item = {"participant": pc, "athlete_id": aid, "round": rnd, "round_start": bool(start),
                "partial_start_reason": start.partial_reason if start else None, "opens_camera_reference": start.opens_camera_reference if start else None,
                "prediction": "prediction" in kinds, "sprint_start": "sprint_start" in kinds, "sprint_end": "sprint_end" in kinds,
                "round_end": bool(end), "invalid": bool(end and end.invalid), "invalid_reason": end.notes if end and end.invalid else None,
                "result": None if res is None else ({"time_s": res.time_s, "method": res.method} if res.time_s is not None else {"missing_reason": res.missing_reason, "notes": res.notes}),
                "timing": (r or {}).get("timing"), "time_increase_pct": (r or {}).get("time_increase_pct"),
                "eligible_for_primary": r is not None and (pc, rnd) not in ineligible, "excluded_because": ineligible.get((pc, rnd))}
        why = []
        if not item["prediction"] and not item["invalid"]:
            why.append("no saved prediction")
        if item["sprint_start"] and res is None and not item["invalid"]:
            why.append("sprint started but no independent result or missing reason was entered")
        if not item["round_end"]:
            why.append("round never ended (no round_end)")
        if item["prediction"] and not item["sprint_start"] and not item["invalid"]:
            why.append("score saved but the sprint was never started")
        item["incomplete"] = why
        if why:
            problems.append(f"{pc} round {rnd}: " + "; ".join(why))
        rounds.append(item)
    if cfg.get("expect"):
        have = Counter(x["participant"] for x in rounds)
        for pc, n in cfg["expect"].items():
            if have.get(pc, 0) < n:
                problems.append(f"{pc}: {have.get(pc, 0)} rounds in the log, {n} expected")
    return {"policy": policy, "rounds": rounds, "eligible": sum(1 for x in rounds if x["eligible_for_primary"]),
            "excluded": [f"{x['participant']} round {x['round']}: {x['excluded_because']}" for x in rounds if x["excluded_because"]],
            "problems": problems}


def step_reproduce(cfg: dict) -> dict:
    from app.db import Database  # noqa: PLC0415
    from app.store.recording_import import import_package  # noqa: PLC0415
    pkg = load(cfg)
    db_path = Path(tempfile.mkdtemp(prefix="athletrix-verify-")) / "fresh.db"
    db = Database(db_path)
    new = import_package(db, pkg, datetime.now(UTC)).recording_id
    db.close()
    rep = subprocess.run([sys.executable, "-c", (
        "import json;from fastapi.testclient import TestClient;from app.db import Database,set_database;from app.main import create_app;"
        f"set_database(Database({str(db_path)!r}));c=TestClient(create_app());"
        f"es=c.get('/api/v1/recordings/{new}/study').json()['entries'];"
        f"print(json.dumps([[e['id'], c.get('/api/v1/recordings/{new}/study/predictions/'+e['id']+'/reproduce').json().get('status')] for e in es if e['kind']=='prediction']))")],
        cwd=ROOT, capture_output=True, text=True, env={**os.environ, "FATIGUE_PROFILE_PATH": ""})
    if rep.returncode != 0:
        raise RuntimeError("reproduction failed: " + rep.stderr[-500:])
    statuses = json.loads(rep.stdout.strip().splitlines()[-1])
    shutil.rmtree(db_path.parent, ignore_errors=True)
    unavailable = Counter(c for e in pkg.study if e.kind == "prediction" for c, v in (((e.prediction or {}).get("result") or {}).get("candidates") or {}).items()
                          if not v.get("available"))
    return {"predictions": len(statuses), "statuses": dict(Counter(s for _, s in statuses)), "all_reproduced": all(s == "reproduced" for _, s in statuses),
            "not_reproduced": [i for i, s in statuses if s != "reproduced"], "deliberately_unavailable_when_saved": dict(unavailable),
            "note": "imported into a fresh temporary database (deleted afterwards); the saved export is unchanged"}


def step_report(cfg: dict) -> dict:
    if os.environ.get("ATHLETRIX_VERIFY_FAIL_REPORT"):
        raise RuntimeError("deliberate report failure (ATHLETRIX_VERIFY_FAIL_REPORT, tests only)")
    out = Path(cfg["out"]) / ("report" if not (Path(cfg["out"]) / "report").exists() else f"report-{datetime.now(UTC):%Y%m%dT%H%M%S}")
    cmd = [sys.executable, str(ROOT / "scripts" / "evaluate.py"), "pilot", "--packages", cfg["saved_package"], "--out", str(out)]
    if cfg.get("manifest"):
        cmd += ["--manifest", cfg["manifest"]]
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0 or not (out / "report.md").exists():
        raise RuntimeError(f"evaluate.py pilot exited {r.returncode}: " + (r.stdout + r.stderr)[-600:])
    return {"report": str(out / "report.md"), "results": str(out / "results.json"), "command": " ".join(cmd)}


FUNCS = {"saved": step_saved, "summary": step_summary, "evidence": step_evidence, "rounds": step_rounds, "reproduce": step_reproduce, "report": step_report}


def verdict(d: Path) -> dict:
    docs = {s: json.loads((d / f"{s}.json").read_text()) if (d / f"{s}.json").exists() else None for s in STEPS}
    failed = [s for s, x in docs.items() if x is None or x.get("status") != "ok"]
    reasons = []
    if docs["evidence"] and docs["evidence"].get("status") == "ok":
        reasons += docs["evidence"]["flags"]
    if docs["rounds"] and docs["rounds"].get("status") == "ok":
        reasons += docs["rounds"]["problems"]
    if docs["reproduce"] and docs["reproduce"].get("status") == "ok" and not docs["reproduce"]["all_reproduced"]:
        reasons.append(f"predictions not reproduced: {docs['reproduce']['not_reproduced']}")
    state = "STEP FAILED" if failed else ("COMPLETE" if not reasons else "INCOMPLETE")
    return {"verdict": state, "failed_or_missing_steps": failed, "reasons": reasons,
            "resume": f".venv/bin/python scripts/session_verify.py --resume {d}" if failed else None}


def main(argv=None, out=print) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--package", type=Path)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--manifest", type=Path)
    ap.add_argument("--expect-rounds", help="participant:rounds pairs, e.g. P1:3,P2:3")
    ap.add_argument("--end-margin-s", type=float, default=10.0)
    ap.add_argument("--resume", type=Path)
    a = ap.parse_args(argv)
    if a.resume:
        d = a.resume
        cfg = json.loads((d / "verify-config.json").read_text())
    else:
        if not a.package or not a.out:
            out("REFUSED: give --package and --out (or --resume DIR)"); return 2
        if not a.package.is_file():
            out(f"REFUSED: {a.package} is not a file"); return 2
        d = a.out
        d.mkdir(parents=True, exist_ok=True)
        cfg = {"package": str(a.package.resolve()), "out": str(d.resolve()), "manifest": str(a.manifest.resolve()) if a.manifest else None,
               "expect": {k: int(v) for k, v in (x.split(":") for x in a.expect_rounds.split(","))} if a.expect_rounds else None,
               "end_margin_s": a.end_margin_s, "created": iso(datetime.now(UTC))}
        write(d / "verify-config.json", cfg)
    for s in STEPS:
        f = d / f"{s}.json"
        if f.exists() and json.loads(f.read_text()).get("status") == "ok":
            if s == "saved":
                cfg["saved_package"] = json.loads(f.read_text())["saved_copy"]
            continue
        try:
            doc = {"status": "ok", **FUNCS[s](cfg)}
        except Exception as exc:  # noqa: BLE001  (a failed step is saved and reported; earlier ones stay)
            write(f, {"status": "failed", "error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()[-2000:]})
            out(f"STEP FAILED: {s}: {exc}")
            if s == "saved":
                break
            continue
        write(f, doc)
        write(d / "verify-config.json", cfg)
    v = verdict(d)
    write(d / "verdict.json", v)
    summ = json.loads((d / "summary.json").read_text()) if (d / "summary.json").exists() else {}
    rec = summ.get("recording") or {}
    prof = ((summ.get("session") or {}).get("profile")) or {}
    out(f"Recording {rec.get('id')} ({rec.get('name')}), stopped {rec.get('stopped_at')}; profile {prof.get('version')} {str(prof.get('sha256'))[:12]}"
        f"{' FICTIONAL' if (summ.get('session') or {}).get('fictional') else ''}")
    if (d / "rounds.json").exists() and json.loads((d / "rounds.json").read_text()).get("status") == "ok":
        r = json.loads((d / "rounds.json").read_text())
        out(f"Rounds: {len(r['rounds'])} in the log, {r['eligible']} eligible for the primary comparison (policy {r['policy']})")
        for x in r["excluded"]:
            out(f"  excluded: {x}")
    if (d / "reproduce.json").exists() and json.loads((d / "reproduce.json").read_text()).get("status") == "ok":
        rp = json.loads((d / "reproduce.json").read_text())
        out(f"Reproduction on a fresh copy: {rp['statuses']} of {rp['predictions']} saved predictions")
    out(f"Saved export: {json.loads((d / 'saved.json').read_text()).get('saved_copy') if (d / 'saved.json').exists() else 'NOT SAVED'}; folder {d}")
    out(f"VERDICT: {v['verdict']}")
    for x in v["reasons"]:
        out(f"  - {x}")
    if v["resume"]:
        out(f"  rerun the failed steps: {v['resume']}")
    return {"COMPLETE": 0, "INCOMPLETE": 1}.get(v["verdict"], 2)


if __name__ == "__main__":
    sys.exit(main())
