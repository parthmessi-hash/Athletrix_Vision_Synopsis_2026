"""Explicit, time-bounded device-to-athlete assignments (a strap can move between athletes; a change is a new row).

FILE (format "athletrix-device-assignments", version 1):
    {"format": "athletrix-device-assignments", "format_version": 1,
     "assignments": [{"device": "strap-07", "athlete_id": "athlete-3", "from": "2026-10-30T10:00:00Z",
                      "to": "2026-10-30T10:45:00Z", "assigned_by": "assistant coach"}]}
`from` is inclusive, `to` exclusive and optional (open-ended). One device may have several non-overlapping rows. A
record whose device has no row covering its observation time is UNASSIGNED: it is never submitted under any athlete
and is counted. Nothing is inferred from device names.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

FORMAT = "athletrix-device-assignments"


class AssignmentError(Exception):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass(frozen=True)
class DeviceAssignment:
    device: str
    athlete_id: str
    start: datetime
    end: datetime | None = None
    assigned_by: str = ""


def _dt(v, what, errors):
    if not isinstance(v, str):
        errors.append(f"{what} must be an ISO 8601 timestamp with a timezone")
        return None
    try:
        d = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        errors.append(f"{what} {v!r} is not a valid timestamp")
        return None
    if d.tzinfo is None:
        errors.append(f"{what} {v!r} has no timezone")
        return None
    return d


def parse(doc: dict) -> list[DeviceAssignment]:
    errors, out = [], []
    if not isinstance(doc, dict) or doc.get("format") != FORMAT or doc.get("format_version") != 1 or not isinstance(doc.get("assignments"), list):
        raise AssignmentError([f"expected format {FORMAT!r} version 1 with an assignments list"])
    for n, a in enumerate(doc["assignments"], start=1):
        if not isinstance(a, dict) or not {"device", "athlete_id", "from"} <= set(a) or not set(a) <= {"device", "athlete_id", "from", "to", "assigned_by"}:
            errors.append(f"assignment {n}: needs device, athlete_id and from (optional to, assigned_by)")
            continue
        if not all(isinstance(a[k], str) and a[k].strip() for k in ("device", "athlete_id")):
            errors.append(f"assignment {n}: device and athlete_id must be non-blank strings")
            continue
        start = _dt(a["from"], f"assignment {n} from", errors)
        end = _dt(a["to"], f"assignment {n} to", errors) if a.get("to") is not None else None
        if start and end and end <= start:
            errors.append(f"assignment {n}: to must be after from")
            continue
        if start:
            out.append(DeviceAssignment(a["device"], a["athlete_id"], start, end, str(a.get("assigned_by", ""))))
    for i, a in enumerate(out):
        for b in out[i + 1:]:
            if a.device == b.device and (a.end is None or b.start < a.end) and (b.end is None or a.start < b.end):
                errors.append(f"device {a.device!r} has overlapping assignments")
    if errors:
        raise AssignmentError(errors)
    return out


def load(path: Path) -> list[DeviceAssignment]:
    return parse(json.loads(Path(path).read_text(encoding="utf-8")))


def resolve(assignments: list[DeviceAssignment], device: str, at: datetime) -> DeviceAssignment | None:
    """The assignment covering this device at this instant, or None (unassigned)."""
    for a in assignments:
        if a.device == device and a.start <= at and (a.end is None or at < a.end):
            return a
    return None
