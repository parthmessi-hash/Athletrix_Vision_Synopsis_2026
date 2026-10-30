"""Two FICTIONAL provider formats that exercise the shared foundation. They imitate no real product.

"fictionstrap-csv" (fictional chest strap, CSV):
    columns  time (ISO 8601 with offset; the strap's clock, stated as UTC), strap, event ("" | "connected" |
             "disconnected"), hr (beats per minute), rmssd (milliseconds; documented by this fictional vendor as RMSSD
             calculated on the strap over 30 s, NOT beat intervals), contact (1 sensor contact, 0 none), battery (percent)
    mapped   heart_rate_bpm <- hr; hrv_rmssd_ms <- rmssd
    quality  contact 0 -> no sample (the strap says it is not touching the body); battery -> device-status history

"imaginaryvest-jsonl" (fictional vest, JSON Lines):
    {"dev", "t_ms" (milliseconds since 1970 on the VEST's clock, which may be offset), "rx" (reader receipt, ISO,
     optional), "type": "data" | "link", "state": "up" | "down" (link records), "hr_bps" (beats per SECOND),
     "br_pm" (breaths per minute), "skin_f" (skin temperature, degrees Fahrenheit), "core_est_c" (ESTIMATED core
     temperature, Celsius), "posture" ("upright" | "seated" | "supine" | "unknown"), "act_g" (activity, g units),
     "sig_pct" (signal quality, percent), "batt" (battery percent)}
    mapped   heart_rate_bpm <- hr_bps x 60; breathing_rate_bpm <- br_pm; skin_temperature_c <- skin_f (F -> C);
             posture <- posture (category table); signal_quality <- sig_pct / 100
    unmapped core_est_c (an estimated core temperature is not a skin temperature, and the model has no core field);
             act_g (activity in g; activity_level is a 0-1 level and this vendor defines no normalisation)
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from wearable.mapping import FieldMap, Mapping
from wearable.model import RawRecord


def _iso(v) -> datetime | None:
    if not isinstance(v, str) or not v.strip():
        return None
    try:
        d = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo is not None else None


class StrapCsvReader:
    provider = "fictionstrap-csv"
    COLUMNS = ("time", "strap", "event", "hr", "rmssd", "contact", "battery")

    def __init__(self, path: Path) -> None:
        self.path, self._problems = Path(path), []

    @property
    def problems(self) -> list[str]:
        return self._problems

    def read(self) -> list[RawRecord]:
        rows = list(csv.reader(io.StringIO(self.path.read_text(encoding="utf-8"))))
        if not rows or tuple(h.strip() for h in rows[0]) != self.COLUMNS:
            self._problems.append(f"line 1: header must be exactly {','.join(self.COLUMNS)}")
            return []
        out = []
        for n, row in enumerate(rows[1:], start=2):
            if not any(c.strip() for c in row):
                continue
            if len(row) != len(self.COLUMNS):
                self._problems.append(f"line {n}: expected {len(self.COLUMNS)} columns, got {len(row)}")
                continue
            r = dict(zip(self.COLUMNS, (c.strip() for c in row)))
            if not r["strap"]:
                self._problems.append(f"line {n}: strap is required")
                continue
            kind = {"": "data", "connected": "connected", "disconnected": "disconnected"}.get(r["event"])
            if kind is None:
                self._problems.append(f"line {n}: event must be empty, connected or disconnected")
                continue
            out.append(RawRecord(self.provider, r["strap"], kind, r["time"], None, {k: r[k] for k in ("hr", "rmssd", "contact", "battery")}, n))
        return out


class VestJsonlReader:
    provider = "imaginaryvest-jsonl"
    KEYS = {"dev", "t_ms", "rx", "type", "state", "hr_bps", "br_pm", "skin_f", "core_est_c", "posture", "act_g", "sig_pct", "batt"}

    def __init__(self, path: Path) -> None:
        self.path, self._problems = Path(path), []

    @property
    def problems(self) -> list[str]:
        return self._problems

    def read(self) -> list[RawRecord]:
        out = []
        for n, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError as exc:
                self._problems.append(f"line {n}: not valid JSON ({exc.msg})")
                continue
            if not isinstance(r, dict) or not {"dev", "t_ms", "type"} <= set(r) or not set(r) <= self.KEYS:
                self._problems.append(f"line {n}: needs dev, t_ms and type; allowed keys {sorted(self.KEYS)}")
                continue
            if r["type"] == "link":
                kind = {"up": "connected", "down": "disconnected"}.get(r.get("state"))
            elif r["type"] == "data":
                kind = "data"
            else:
                kind = None
            if kind is None:
                self._problems.append(f"line {n}: type must be data, or link with state up/down")
                continue
            received = _iso(r.get("rx")) if r.get("rx") is not None else None
            if r.get("rx") is not None and received is None:
                self._problems.append(f"line {n}: rx must be ISO 8601 with a timezone")
                continue
            fields = {k: v for k, v in r.items() if k not in ("dev", "t_ms", "rx", "type", "state")}
            out.append(RawRecord(self.provider, str(r["dev"]), kind, r["t_ms"], received, fields, n))
        return out


def strap_time(observed) -> datetime | None:
    return _iso(observed)


def vest_time(observed) -> datetime | None:
    if isinstance(observed, bool) or not isinstance(observed, (int, float)):
        return None
    return datetime.fromtimestamp(observed / 1000.0, tz=UTC)    # the vest's own clock, expressed as an epoch; may be offset


@dataclass(frozen=True)
class ProviderAdapter:
    name: str
    reader: type
    mapping: Mapping
    parse_time: object
    worn_field: str | None = None          # a field whose value 0/false means "not on the body": no sample
    battery_field: str | None = None
    signal_field: str | None = None
    signal_scale: float = 1.0              # provider signal value * scale -> DeviceStatus.signal_quality (0-1)


STRAP = ProviderAdapter(
    "fictionstrap-csv", StrapCsvReader,
    Mapping("fictionstrap-csv", "1", (FieldMap("heart_rate_bpm", "hr", note="beats per minute"),
                                      FieldMap("hrv_rmssd_ms", "rmssd", note="the fictional vendor documents this as RMSSD over 30 s, in ms; it is not beat intervals")),
            quality={"contact": "1 = sensor contact, 0 = none (no sample)", "battery": "percent, device-status history"}),
    strap_time, worn_field="contact", battery_field="battery")

VEST = ProviderAdapter(
    "imaginaryvest-jsonl", VestJsonlReader,
    Mapping("imaginaryvest-jsonl", "1", (
        FieldMap("heart_rate_bpm", "hr_bps", "per_second_to_per_minute", note="the vest reports beats per second"),
        FieldMap("breathing_rate_bpm", "br_pm"),
        FieldMap("skin_temperature_c", "skin_f", "fahrenheit_to_celsius", note="skin temperature only"),
        FieldMap("posture", "posture", "category", {"upright": "standing", "seated": "sitting", "supine": "lying", "unknown": "unknown"}),
        FieldMap("signal_quality", "sig_pct", "percent_to_fraction")),
        unmapped={"core_est_c": "an ESTIMATED core temperature is not a skin temperature; PhysiologicalSample has no core temperature field",
                  "act_g": "activity in g units; activity_level is a normalised 0-1 level and the vendor defines no normalisation"},
        quality={"sig_pct": "signal quality percent (also mapped)", "batt": "battery percent, device-status history"}),
    vest_time, battery_field="batt", signal_field="sig_pct", signal_scale=0.01)

ADAPTERS = {a.name: a for a in (STRAP, VEST)}
