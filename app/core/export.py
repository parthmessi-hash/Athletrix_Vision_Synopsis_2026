"""Export of an athlete's retained samples in the JSON Lines interchange format replayed by scripts/replay_samples.py.

A snapshot of what the engine retains right now for one athlete (newest 300 per stream): not a recording archive, not
a database backup, and nothing already trimmed by the retention cap can come back. Records are written exactly as
stored (timestamps with their recorded offsets, identifiers, sources, optional measurements, nulls, zeros,
confidences) and nothing is rebased or invented.
"""

import json
import re
from collections.abc import Sequence
from datetime import datetime

from app.models import PhysiologicalSample, TrackingSample

STREAM_ORDER = {"physiological": 0, "tracking": 1}


def export_records(physiological: Sequence[PhysiologicalSample], tracking: Sequence[TrackingSample]) -> list[dict]:
    """Interchange records for both streams, oldest first; at equal instants physiological precedes tracking.

    Within one stream an athlete has at most one row per instant, so (instant, stream) is a total, deterministic order.
    """
    rows: list[tuple[datetime, int, dict]] = []
    for s in physiological:
        rows.append((s.timestamp, STREAM_ORDER["physiological"], {"type": "physiological", "sample": s.model_dump(mode="json")}))
    for s in tracking:
        rows.append((s.timestamp, STREAM_ORDER["tracking"], {"type": "tracking", "sample": s.model_dump(mode="json")}))
    rows.sort(key=lambda r: (r[0], r[1]))  # aware datetimes compare as instants, so offsets do not affect the order
    return [r[2] for r in rows]


def to_jsonl(records: Sequence[dict]) -> str:
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)


def export_filename(athlete_id: str, captured_at: datetime) -> str:
    """A safe download name: the athlete id reduced to [A-Za-z0-9._-] and the UTC capture time."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", athlete_id).strip("._") or "athlete"
    stamp = captured_at.strftime("%Y%m%dT%H%M%SZ")
    return f"athletrix-retained-samples-{safe}-{stamp}.jsonl"
