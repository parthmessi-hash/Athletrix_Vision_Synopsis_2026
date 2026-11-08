"""Independent 20 m sprint timing for the fatigue pilot: a blind round sheet, validation with a preview, and import.

The timer never sees a score: the sheet comes from GET /api/v1/recordings/{id}/study/timing-sheet, which holds the
wearer code, round and start / end markers only.

USAGE (default engine http://127.0.0.1:8000, default recording: the active one)
    python scripts/sprint_timing.py sheet --out times.csv
    python scripts/sprint_timing.py validate times.csv [--video-dir DIR]        # preview; changes nothing
    python scripts/sprint_timing.py apply times.csv --yes [--video-dir DIR]      # stores new results and corrections
COLUMNS (the sheet writes them; leave a cell empty when it does not apply)
    participant_code, round, sprint_start_marker (read-only), method (timing_gates | stopwatch | video_frames | other),
    time_s, precision_s, timing_uncertainty_s, video_file, video_sha256, start_pts_s, finish_pts_s,
    gate_start_device_time, gate_clock_offset_s, gate_clock_uncertainty_s, invalid_reason, missing_reason, timer, notes,
    correction_reason
METHODS
    timing_gates  read the gate display: time_s and the gate's stated precision_s; optionally the gate's start time on its
                  own clock (gate_start_device_time) and, from a recorded clock check, its offset to the engine clock and
                  that offset's uncertainty (without them the start-before-save order stays unverified).
    video_frames  from a LOCAL clip (never copied into a package): start_pts_s and finish_pts_s are presentation timestamps
                  READ FROM THE FILE for the start and finish frames (e.g. `ffprobe -select_streams v -show_entries
                  frame=pts_time -of csv VIDEO`), never frame number / nominal frame rate; timing_uncertainty_s covers half
                  the actual interval to the neighbouring frames plus the reviewer's judgement. time_s = finish - start.
                  With --video-dir the file's SHA-256 is computed locally and checked against video_sha256; a missing or
                  unreadable clip makes the row a MISSING result ("clip unavailable"), never a guessed time.
ROW STATUS IN THE PREVIEW
    new, unchanged (identical to what is stored), correction (differs from the stored result: needs correction_reason),
    duplicate (the same wearer and round twice in the sheet), ambiguous (unknown wearer or round, or no start marker),
    invalid (bad values). Only new and complete corrections are applied; everything else is listed and skipped.
Exit codes: 0 nothing refused, 1 some rows need attention (listed), 2 invalid file, 3 engine unreachable.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path

import httpx

COLUMNS = ["participant_code", "round", "sprint_start_marker", "method", "time_s", "precision_s", "timing_uncertainty_s", "video_file", "video_sha256", "start_pts_s",
           "finish_pts_s", "gate_start_device_time", "gate_clock_offset_s", "gate_clock_uncertainty_s", "invalid_reason", "missing_reason", "timer", "notes", "correction_reason"]
MISSING = ("not_collected", "declined", "not_recorded", "invalid_attempt", "equipment_failure", "other")


def num(v):
    return None if v in (None, "") else float(v)


def to_body(row: dict, rnd: dict, video_dir: Path | None) -> tuple[dict | None, str | None]:
    """The sprint_result body for one sheet row, or (None, why it cannot be used)."""
    b = {"kind": "sprint_result", "athlete_id": rnd["athlete_id"], "round": rnd["round"], "recorded_by": row.get("timer") or None, "notes": row.get("notes") or None}
    try:
        if row.get("invalid_reason"):
            b.update(missing_reason="invalid_attempt", notes=("invalid: " + row["invalid_reason"] + (f"; {row['notes']}" if row.get("notes") else "")))
            return b, None
        if row.get("missing_reason"):
            if row["missing_reason"] not in MISSING:
                return None, f"missing_reason must be one of {', '.join(MISSING)}"
            b.update(missing_reason=row["missing_reason"])
            return b, None
        method = row.get("method")
        if method not in ("timing_gates", "stopwatch", "video_frames", "other"):
            return None, "method must be timing_gates, stopwatch, video_frames or other (or give missing_reason / invalid_reason)"
        b.update(method=method, precision_s=num(row.get("precision_s")), timing_uncertainty_s=num(row.get("timing_uncertainty_s")))
        if method == "video_frames":
            if row.get("video_file") and video_dir is not None:
                f = video_dir / row["video_file"]
                try:
                    digest = hashlib.sha256(f.read_bytes()).hexdigest()
                except OSError:
                    b = {k: v for k, v in b.items() if k not in ("method", "precision_s", "timing_uncertainty_s")}
                    b.update(missing_reason="other", notes=f"video clip unavailable or unreadable: {row['video_file']}")
                    return b, None
                if row.get("video_sha256") and row["video_sha256"] != digest:
                    return None, f"video_sha256 does not match {row['video_file']}"
                row = {**row, "video_sha256": digest}
            s0, s1 = num(row.get("start_pts_s")), num(row.get("finish_pts_s"))
            if s0 is None or s1 is None or b["timing_uncertainty_s"] is None:
                return None, "video timing needs start_pts_s, finish_pts_s (read from the file) and timing_uncertainty_s"
            t = round(s1 - s0, 4)
            if row.get("time_s") and abs(float(row["time_s"]) - t) > 0.002:
                return None, f"time_s {row['time_s']} differs from finish - start ({t})"
            b.update(time_s=t, start_pts_s=s0, finish_pts_s=s1, video_name=row.get("video_file") or None, video_sha256=row.get("video_sha256") or None)
        else:
            if not row.get("time_s"):
                return None, "time_s is empty (or give missing_reason / invalid_reason)"
            b.update(time_s=float(row["time_s"]))
        if row.get("gate_start_device_time"):
            b["start_reference"] = {"source": "timing_gate" if method != "video_frames" else "video", "device_time": row["gate_start_device_time"],
                                    "clock_offset_s": num(row.get("gate_clock_offset_s")), "clock_uncertainty_s": num(row.get("gate_clock_uncertainty_s"))}
    except ValueError as exc:
        return None, f"not a number: {exc}"
    return {k: v for k, v in b.items() if v is not None}, None


def same(stored: dict | None, body: dict) -> bool:
    if not stored:
        return False
    keys = ("time_s", "method", "missing_reason", "precision_s", "timing_uncertainty_s", "video_sha256", "start_pts_s", "finish_pts_s")
    return all(stored.get(k) == body.get(k) for k in keys) and (stored.get("notes") or None) == (body.get("notes") or None)


def plan(sheet_rows: list[dict], timing: dict, video_dir: Path | None) -> list[dict]:
    rounds = {(r["participant_code"], str(r["round"])): r for r in timing["rounds"]}
    seen: dict[tuple, int] = {}
    for row in sheet_rows:
        k = (row.get("participant_code"), str(row.get("round")))
        seen[k] = seen.get(k, 0) + 1
    out = []
    for n, row in enumerate(sheet_rows, start=2):
        k = (row.get("participant_code"), str(row.get("round")))
        item = {"line": n, "participant_code": k[0], "round": k[1]}
        if not any(row.get(c) for c in ("time_s", "start_pts_s", "missing_reason", "invalid_reason")):
            out.append({**item, "status": "blank"}); continue
        if seen[k] > 1:
            out.append({**item, "status": "duplicate", "why": "the same wearer and round appear more than once"}); continue
        rnd = rounds.get(k)
        if rnd is None or not rnd.get("sprint_start_marker"):
            out.append({**item, "status": "ambiguous", "why": "no such wearer and round with a sprint start in this recording"}); continue
        body, why = to_body(row, rnd, video_dir)
        if body is None:
            out.append({**item, "status": "invalid", "why": why}); continue
        stored = rnd.get("result")
        if stored is None:
            out.append({**item, "status": "new", "body": body})
        elif same(stored, body):
            out.append({**item, "status": "unchanged"})
        elif not row.get("correction_reason"):
            out.append({**item, "status": "correction", "why": f"differs from the stored result {stored['id']} ({stored.get('time_s') or stored.get('missing_reason')}): add correction_reason to replace it",
                        "applicable": False})
        else:
            out.append({**item, "status": "correction", "body": {**body, "supersedes": stored["id"], "correction_reason": row["correction_reason"]}, "applicable": True})
    return out


def main(argv=None, out=print, client: httpx.Client | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--engine", default="http://127.0.0.1:8000")
    ap.add_argument("--recording")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("sheet"); p.add_argument("--out", type=Path, required=True)
    for name in ("validate", "apply"):
        p = sub.add_parser(name); p.add_argument("sheet", type=Path); p.add_argument("--video-dir", type=Path)
        if name == "apply":
            p.add_argument("--yes", action="store_true", required=True)
    args = ap.parse_args(argv)
    e = args.engine.rstrip("/") + "/api/v1"
    own = client is None
    c = client or httpx.Client(timeout=15.0)
    try:
        rid = args.recording or (c.get(f"{e}/recordings/active").json().get("recording") or {}).get("id")
        if not rid:
            out("No active recording: pass --recording.")
            return 2
        timing = c.get(f"{e}/recordings/{rid}/study/timing-sheet").json()
        if args.cmd == "sheet":
            if args.out.exists():
                out(f"{args.out} exists; not overwritten")
                return 2
            with open(args.out, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=COLUMNS)
                w.writeheader()
                for r in timing["rounds"]:
                    if r.get("sprint_start_marker"):
                        w.writerow({"participant_code": r["participant_code"], "round": r["round"], "sprint_start_marker": r["sprint_start_marker"]})
            out(f"wrote {args.out}: {sum(1 for r in timing['rounds'] if r.get('sprint_start_marker'))} rounds (no scores; blind)" + (" · FICTIONAL session" if timing["fictional"] else ""))
            return 0
        try:
            with open(args.sheet, newline="") as f:
                rows = list(csv.DictReader(f))
        except OSError as exc:
            out(f"Error: {exc}")
            return 2
        if not rows or set(COLUMNS[:2]) - set(rows[0]):
            out("Error: not a timing sheet (participant_code and round columns are required)")
            return 2
        items = plan(rows, timing, args.video_dir)
        for it in items:
            out(f"  line {it['line']}: {it['participant_code']} round {it['round']}: {it['status']}" + (f" ({it['why']})" if it.get("why") else "") +
                (f" -> {json.dumps({k: v for k, v in it['body'].items() if k in ('time_s', 'method', 'missing_reason', 'supersedes')})}" if it.get("body") else ""))
        todo = [it for it in items if it["status"] == "new" or (it["status"] == "correction" and it.get("applicable"))]
        attention = [it for it in items if it["status"] in ("duplicate", "ambiguous", "invalid") or (it["status"] == "correction" and not it.get("applicable"))]
        if args.cmd == "validate":
            out(f"{len(todo)} to apply, {len(attention)} need attention, {sum(1 for it in items if it['status'] == 'unchanged')} unchanged (nothing was stored)")
            return 1 if attention else 0
        stored = 0
        for it in todo:
            tok = "timing-" + hashlib.sha256(json.dumps([rid, it["body"]], sort_keys=True).encode()).hexdigest()[:32]
            r = c.post(f"{e}/recordings/{rid}/study", json={**it["body"], "client_token": tok})
            if r.status_code in (200, 201):
                stored += r.status_code == 201
            else:
                out(f"  line {it['line']}: refused by the engine: {r.text[:200]}")
                attention.append(it)
        out(f"stored {stored} result(s); {len(attention)} row(s) need attention")
        return 1 if attention else 0
    except httpx.HTTPError as exc:
        out(f"The engine at {args.engine} is not reachable ({exc}); nothing was stored.")
        return 3
    finally:
        if own:
            c.close()


if __name__ == "__main__":
    sys.exit(main())
