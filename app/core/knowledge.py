"""When evidence was known, as opposed to when it happened (pure helpers).

Every review time T has two readings:
  retrospective  (default) everything stored now about what happened up to T: annotations entered after T, corrections
                 entered after T, context and roster entries entered after T, baselines created or accepted after T. Such
                 a result is labelled retrospective and counts what was entered later: it is NOT what a live system
                 could have shown at T.
  as_entered     only evidence that had been entered by T: observations, corrections, context and roster entries with
                 created_at <= T, baselines created by T with the review status their recorded decisions gave them at T
                 (pending_review before the first decision). A correction entered after T is ignored, so the observation
                 it later replaced counts, as it would have then.
Recorded samples are judged by their receipt time where it was recorded (app/core/physiology_index.py); a sample
without receipt metadata is assumed available at its own timestamp, which the result states.
"""

from __future__ import annotations

from datetime import datetime

MODES = ("retrospective", "as_entered")


def entered_by(items: list, t: datetime) -> list:
    """Items (observations, context or roster entries) entered at or before t."""
    return [x for x in items if x.created_at <= t]


def baselines_as_of(baselines: list, t: datetime) -> list:
    """Baselines created by t, each with the status its decisions recorded by t gave it."""
    out = []
    for b in baselines:
        if b.created_at > t:
            continue
        known = [d for d in b.decisions if d.decided_at <= t]
        out.append(b.model_copy(update={"decisions": known, "status": known[-1].decision if known else "pending_review"}))
    return out


def entered_later(items: list, t: datetime) -> list:
    return [x for x in items if x.created_at > t]


def status_changed_later(baseline, t: datetime) -> bool:
    """True when the baseline was created after t or its current status was decided after t."""
    return baseline.created_at > t or any(d.decided_at > t for d in baseline.decisions)
