"""Time alignment between one stream's clock and a reference (UTC) from operator-supplied evidence (pure functions).

BASIS (stated, never inferred):
  capture_time_mapping  the stream's own clock was measured against the reference (documented clock measurements, e.g.
                        a camera filming a UTC clock at the start and end)
  operator_estimate     the operator matched reference events seen in the stream (whistle, clap, a flash) to reference times
  replay_anchor         only a playback anchor ("video second 0 is 10:00:00"); NOT capture time
  unknown               nothing stated
Timestamps that merely look close are never treated as synchronized.

FIT. Pairs (stream time s_i in seconds, reference r_i): with fewer than 3 pairs, or when the profile asks for it, an
offset only (r = s + offset, the mean of r_i - s_i); with 3 or more pairs spanning at least `min_drift_span_s`, offset and
drift (r = s + offset + drift * (s - s0)) by least squares. Residuals are reported per event; an event whose residual
is far from the others (more than max(5 x the median absolute residual, tolerance_s)) is flagged as possibly MISMATCHED
and is never dropped automatically: the operator excludes it by label and refits. Uncertainty is the largest
leave-one-out prediction error (how far each event is from a fit made without it) plus the stated reference
uncertainty; with one pair it is only the stated reference uncertainty and the result says that drift is unknown.
Mapped times always keep the original stream time next to the mapped reference time.
"""

from __future__ import annotations

import json
import math
import statistics
from datetime import UTC, datetime, timedelta
from pathlib import Path

FORMAT = "athletrix-time-alignment"
BASES = ("capture_time_mapping", "operator_estimate", "replay_anchor", "unknown")


class AlignmentError(ValueError):
    pass


def _utc(v: str) -> datetime:
    t = datetime.fromisoformat(v.replace("Z", "+00:00"))
    if t.tzinfo is None:
        raise AlignmentError(f"reference time {v!r} has no timezone")
    return t.astimezone(UTC)


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _linfit(xs, ys):
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx if sxx else 0.0
    return my - b * mx, b


def fit(events: list[dict], *, basis: str, stream: str, stream_id: str = "", reference_uncertainty_s: float = 0.0, tolerance_s: float = 0.1,
        min_drift_span_s: float = 600.0, allow_drift: bool = True, exclude: list[str] | None = None) -> dict:
    """events: [{label, stream_time_s, reference_utc, note?}] -> alignment document (see the module docstring)."""
    if basis not in BASES:
        raise AlignmentError(f"basis must be one of {', '.join(BASES)}")
    if basis in ("replay_anchor", "unknown"):
        if basis == "replay_anchor" and len(events) != 1:
            raise AlignmentError("a replay anchor is exactly one (stream time, reference time) pair")
    elif not events:
        raise AlignmentError(f"basis {basis} needs at least one reference event or clock measurement")
    exclude = set(exclude or [])
    labels = [e.get("label", "") for e in events]
    if len(set(labels)) != len(labels):
        raise AlignmentError("event labels must be distinct")
    unknown_ex = exclude - set(labels)
    if unknown_ex:
        raise AlignmentError(f"excluded labels not found: {', '.join(sorted(unknown_ex))}")
    used = [e for e in events if e.get("label", "") not in exclude]
    if events and not used:
        raise AlignmentError("every event is excluded")
    doc = {"format": FORMAT, "format_version": 1, "stream": stream, "stream_id": stream_id, "basis": basis, "events": events,
           "excluded": sorted(exclude), "reference_uncertainty_s": reference_uncertainty_s, "tolerance_s": tolerance_s,
           "allow_drift": allow_drift, "min_drift_span_s": min_drift_span_s}
    if not used:
        return {**doc, "model": None, "residuals": [], "uncertainty_s": None, "warnings": ["no alignment: the stream's own timeline is kept"]}
    s = [float(e["stream_time_s"]) for e in used]
    r0 = _utc(used[0]["reference_utc"])
    r = [(_utc(e["reference_utc"]) - r0).total_seconds() for e in used]
    span = max(s) - min(s)
    drift_ok = allow_drift and len(used) >= 3 and span >= min_drift_span_s and basis != "replay_anchor"
    if drift_ok:
        s0 = min(s)
        a, b = _linfit([x - s0 for x in s], [y - x for x, y in zip(s, r)])
        model = {"kind": "offset_drift", "origin_stream_s": s0, "offset_s": a, "drift": b, "reference_zero_utc": _iso(r0)}
    else:
        model = {"kind": "offset", "origin_stream_s": 0.0, "offset_s": statistics.fmean(y - x for x, y in zip(s, r)), "drift": 0.0, "reference_zero_utc": _iso(r0)}
    model["stream_zero_utc"] = _iso(r0 + timedelta(seconds=model["offset_s"] - model["drift"] * model["origin_stream_s"]))
    model["note"] = "mapped = reference_zero_utc + s + offset_s + drift x (s - origin_stream_s); stream_zero_utc is what stream time 0 maps to"
    pred = lambda m, x: x + m["offset_s"] + m["drift"] * (x - m["origin_stream_s"])  # noqa: E731
    res = [{"label": e.get("label", ""), "stream_time_s": x, "residual_s": round(y - pred(model, x), 6)} for e, x, y in zip(used, s, r)]
    warnings: list[str] = []
    loo, suspects = [], []
    for i in range(len(used) if len(used) >= 2 else 0):
        xs, ys = s[:i] + s[i + 1:], r[:i] + r[i + 1:]
        if model["kind"] == "offset_drift" and len(xs) >= 2:
            a, b = _linfit([x - model["origin_stream_s"] for x in xs], [y - x for x, y in zip(xs, ys)])
            m = {**model, "offset_s": a, "drift": b}
        else:
            m = {**model, "offset_s": statistics.fmean(y - x for x, y in zip(xs, ys)), "drift": 0.0}
        err = abs(r[i] - pred(m, s[i]))
        loo.append(err)
        others = max(abs(y - pred(m, x)) for x, y in zip(xs, ys))
        if len(used) >= 3 and err > max(tolerance_s, 5 * others):
            suspects.append(used[i].get("label", ""))
    for x in res:
        x["possibly_mismatched"] = x["label"] in suspects
    if suspects:
        warnings.append(f"events {', '.join(suspects)} disagree with a fit of the other events by more than {tolerance_s:g} s (and 5x their own spread): "
                        "possibly mismatched; check them and exclude by label to refit")
    if model["kind"] == "offset" and len(used) >= 2 and span > 0:
        _, slope = _linfit(s, [y - x for x, y in zip(s, r)])
        if abs(slope) * span > tolerance_s:
            warnings.append(f"the offset changes by {abs(slope) * span:.3f} s across the events: clock drift is likely; supply 3+ events spanning at least {min_drift_span_s:g} s")
    if len(used) == 1:
        warnings.append("one event: the offset is known only at that instant; drift is unknown")
    unc = (max(loo) if loo else 0.0) + reference_uncertainty_s
    if basis == "replay_anchor":
        warnings.append("replay anchor: playback time, not capture time; cross-stream comparison is unavailable")
    return {**doc, "model": model, "residuals": res, "rms_s": round(math.sqrt(statistics.fmean(x["residual_s"] ** 2 for x in res)), 6),
            "max_abs_residual_s": round(max(abs(x["residual_s"]) for x in res), 6), "uncertainty_s": round(unc, 6),
            "uncertainty_basis": ("largest leave-one-out error + stated reference uncertainty" if loo else "stated reference uncertainty only (one event)"),
            "span_s": span, "warnings": warnings}


def map_time(doc: dict, stream_time_s: float) -> dict:
    """Original stream time and mapped reference time, never one without the other."""
    m = doc.get("model")
    if not m:
        return {"stream_time_s": stream_time_s, "mapped_utc": None, "reason": "no alignment"}
    t = _utc(m["reference_zero_utc"]) + timedelta(seconds=stream_time_s + m["offset_s"] + m["drift"] * (stream_time_s - m["origin_stream_s"]))
    return {"stream_time_s": stream_time_s, "mapped_utc": _iso(t), "uncertainty_s": doc.get("uncertainty_s"), "basis": doc["basis"]}


def cross_stream(a: dict | None, b: dict | None, needed_s: float) -> dict:
    """Can two streams be compared at a resolution of needed_s? Only with stated alignments whose combined uncertainty fits."""
    why = []
    for name, d in (("first", a), ("second", b)):
        if d is None or d.get("basis") in (None, "unknown") or not d.get("model"):
            why.append(f"the {name} stream has no stated alignment")
        elif d["basis"] == "replay_anchor":
            why.append(f"the {name} stream has only a replay anchor (not capture time)")
    if why:
        return {"available": False, "reasons": why + ["timelines are kept independent"]}
    total = (a["uncertainty_s"] or 0) + (b["uncertainty_s"] or 0)
    if total > needed_s:
        return {"available": False, "reasons": [f"combined alignment uncertainty {total:.3f} s exceeds the {needed_s:g} s this comparison needs"]}
    return {"available": True, "combined_uncertainty_s": round(total, 6), "reasons": [f"both streams aligned ({a['basis']}, {b['basis']}); combined uncertainty {total:.3f} s"]}


def describe(d: dict) -> str:
    if not d.get("model"):
        return f"time alignment ({d['stream']}): none; timelines stay independent"
    return (f"time alignment ({d['stream']} {d.get('stream_id') or ''}): {d['basis'].replace('_', ' ')}, {d['model']['kind']}, stream time 0 = {d['model']['stream_zero_utc']}"
            + (f", drift {d['model']['drift'] * 1e6:.1f} ppm" if d['model']['kind'] == "offset_drift" else "") + f", uncertainty ±{d['uncertainty_s']:.3f} s"
            + (f"; {len(d['warnings'])} warning(s)" if d.get("warnings") else ""))


def load(path: Path) -> dict:
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AlignmentError(f"{Path(path).name}: {exc}") from exc
    if not isinstance(d, dict) or d.get("format") != FORMAT or d.get("format_version") != 1:
        raise AlignmentError(f"{Path(path).name}: not an {FORMAT} version 1 file")
    again = fit(d["events"], basis=d["basis"], stream=d["stream"], stream_id=d.get("stream_id", ""), reference_uncertainty_s=d.get("reference_uncertainty_s", 0.0),
                tolerance_s=d.get("tolerance_s", 0.1), exclude=d.get("excluded"), allow_drift=d.get("allow_drift", True), min_drift_span_s=d.get("min_drift_span_s", 600.0))
    if d.get("model") and again.get("model") and abs(again["model"]["offset_s"] - d["model"]["offset_s"]) > 1e-6:
        raise AlignmentError(f"{Path(path).name}: the stored model does not match its events (edited file)")
    return d
