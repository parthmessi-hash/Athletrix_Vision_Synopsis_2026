"""Normalization: raw provider records -> PhysiologicalSample records, a provenance record per sample, a device-status
history, and an account of everything that was not turned into a sample.

Per record, in the order the reader produced them:
  1. observation time: parsed by the provider adapter and mapped to UTC by the ClockMapping (never guessed); a record
     whose time cannot be parsed is "invalid_time".
  2. connection events ("connected"/"disconnected") go to the device-status history only. No sample is ever made for
     a disconnected period, and nothing is interpolated across it. Data arriving after a "disconnected" event without a
     "connected" one is kept and counted as "data_while_marked_disconnected".
  3. battery and signal fields of a data record are added to the device-status history (a history the engine does not
     keep: it stores only each device's latest status).
  4. a worn/contact flag saying the device is not on the body -> "not_worn", no sample.
  5. the device-to-athlete assignment at the observation time; none -> "unassigned", no sample.
  6. the field mapping: invalid values become null with an issue; unmapped fields are listed in provenance only. A
     record with no mapped value at all -> "no_measurement", no sample.
  7. duplicates by (device, observation time): identical values -> "duplicate" (dropped); different values ->
     "conflicting_duplicate" (the first is kept, the other dropped, both recorded).
  8. ordering: records may arrive late or out of order. Samples are emitted sorted by observation time; the account
     counts records that arrived out of order and the largest receipt delay when receipt times are known.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.models import DeviceStatus, PhysiologicalSample
from wearable import mapping as mp
from wearable.assignments import DeviceAssignment, resolve
from wearable.clock import ClockMapping
from wearable.model import RawRecord


def iso(ts: datetime) -> str:
    return ts.isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass
class NormalizedResult:
    samples: list[dict] = field(default_factory=list)          # PhysiologicalSample payloads, sorted by timestamp
    provenance: list[dict] = field(default_factory=list)       # one per sample, same order
    status_history: list[dict] = field(default_factory=list)   # DeviceStatus payloads plus "event"
    skipped: dict = field(default_factory=dict)                # reason -> count
    issues: list[dict] = field(default_factory=list)           # value-level problems (record kept, value null)
    conflicts: list[dict] = field(default_factory=list)
    ordering: dict = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


def _falsy(v) -> bool:
    return v is not None and str(v).strip().lower() in ("0", "false", "no")


def normalize(records: list[RawRecord], adapter, assignments: list[DeviceAssignment], clock: ClockMapping, *, source: str = "wearable") -> NormalizedResult:
    errors = clock.validate() + adapter.mapping.validate()
    if errors:
        raise ValueError("; ".join(errors))
    res = NormalizedResult()
    seen: dict[tuple, tuple[dict, int]] = {}
    last_ts: dict[str, datetime] = {}
    disconnected: set[str] = set()
    out_of_order, max_delay = 0, None
    staged = []
    for r in records:
        device_time = adapter.parse_time(r.observed)
        try:
            ts = clock.to_utc(device_time, r.received_at)
        except ValueError:
            res.skip("invalid_time")
            res.issues.append({"line": r.line, "device": r.device, "reason": "observation time missing or unparseable", "raw": r.observed})
            continue
        if r.received_at is not None:
            delay = (r.received_at - ts).total_seconds()
            max_delay = delay if max_delay is None else max(max_delay, delay)
        if r.device in last_ts and ts < last_ts[r.device]:
            out_of_order += 1
        last_ts[r.device] = max(ts, last_ts.get(r.device, ts))
        if r.kind != "data":
            connected = r.kind == "connected"
            (disconnected.discard if connected else disconnected.add)(r.device)
            res.status_history.append({**DeviceStatus(device_id=r.device, timestamp=ts, connected=connected).model_dump(mode="json"), "event": r.kind, "line": r.line})
            continue
        if r.device in disconnected:
            res.skip("data_while_marked_disconnected")      # counted, and the record is still processed below
            disconnected.discard(r.device)
        battery = mp._number(r.fields.get(adapter.battery_field)) if adapter.battery_field else None
        signal = mp._number(r.fields.get(adapter.signal_field)) if adapter.signal_field else None
        if battery is not None or signal is not None:
            st = {"device_id": r.device, "timestamp": ts, "connected": True}
            if battery is not None and 0 <= battery <= 100:
                st["battery_percent"] = battery
            if signal is not None and 0 <= signal * adapter.signal_scale <= 1:
                st["signal_quality"] = round(signal * adapter.signal_scale, 6)
            res.status_history.append({**DeviceStatus.model_validate(st).model_dump(mode="json"), "event": "status_in_data", "line": r.line})
        if adapter.worn_field and _falsy(r.fields.get(adapter.worn_field)):
            res.skip("not_worn")
            continue
        asg = resolve(assignments, r.device, ts)
        if asg is None:
            res.skip("unassigned")
            continue
        values, issues = mp.apply(adapter.mapping, r.fields)
        for i in issues:
            res.issues.append({"line": r.line, "device": r.device, **i})
        if not values:
            res.skip("no_measurement")
            continue
        key = (r.device, iso(ts))
        if key in seen:
            first, first_line = seen[key]
            if first == values:
                res.skip("duplicate")
            else:
                res.skip("conflicting_duplicate")
                res.conflicts.append({"device": r.device, "timestamp": iso(ts), "kept_line": first_line, "dropped_line": r.line, "kept": first, "dropped": values})
            continue
        seen[key] = (values, r.line)
        sample = PhysiologicalSample.model_validate({"athlete_id": asg.athlete_id, "timestamp": ts, "source": source, "device_id": r.device, **values}).model_dump(mode="json", exclude_none=True)
        present_unmapped = {k: r.fields[k] for k in adapter.mapping.unmapped if k in r.fields and r.fields[k] is not None}
        prov = {"timestamp": iso(ts), "athlete_id": asg.athlete_id, "device": r.device, "provider": r.provider, "mapping_version": adapter.mapping.version,
                "line": r.line, "observed_raw": r.observed, "received_at": iso(r.received_at) if r.received_at else None,
                "clock": {"basis": clock.basis, "uncertainty_s": clock.uncertainty_s if clock.uncertainty_s is not None else "unknown"},
                "assignment": {"athlete_id": asg.athlete_id, "assigned_by": asg.assigned_by, "from": iso(asg.start), "to": iso(asg.end) if asg.end else None},
                "mapped_from": {m.target: {"source": m.source, "raw": r.fields.get(m.source), "conversion": m.conversion} for m in adapter.mapping.fields if m.target in values},
                "unmapped_present": present_unmapped}
        staged.append((ts, r.line, sample, prov))
    staged.sort(key=lambda x: (x[0], x[1]))
    res.samples = [s for _, _, s, _ in staged]
    res.provenance = [p for _, _, _, p in staged]
    res.status_history.sort(key=lambda s: (s["timestamp"], s["line"]))
    res.ordering = {"records_out_of_order": out_of_order, "largest_receipt_delay_s": None if max_delay is None else round(max_delay, 3),
                    "note": "samples are emitted in observation-time order; receipt delay is known only for records with a receipt time"}
    return res
