"""Declarative field/unit mapping from a provider's fields into PhysiologicalSample, documented and validated.

A mapping lists, for each PhysiologicalSample field it fills, the provider field it comes from and an explicit unit
conversion. Nothing is mapped by name similarity: a provider field that is not listed is UNMAPPED and is kept only in
the provenance record, never placed in a sample. In particular a mapping must not
  - put a device-calculated HRV value into hrv_rmssd_ms or hrv_sdnn_ms unless the provider documents which statistic
    it is (RMSSD or SDNN) and its unit; a calculated HRV value is never treated as raw beat-to-beat intervals;
  - put an estimated core temperature (or any temperature other than skin) into skin_temperature_c;
  - rescale an activity measure (for example g units) into activity_level, which is a 0-1 normalised level, unless the
    provider defines that normalisation.

Values are never clamped or filled: a missing or empty provider value leaves the field null; a value that is not a
number, not finite, outside the model's range after conversion, or not in a category table becomes null with a
recorded issue (field, raw value, reason). A zero is kept wherever the model allows it.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field

from app.models import PhysiologicalSample

# PhysiologicalSample measurement fields and their documented units/ranges (from the model itself).
TARGETS = {name: f for name, f in PhysiologicalSample.model_fields.items()
           if name not in ("athlete_id", "timestamp", "source", "device_id")}

CONVERSIONS: dict[str, Callable[[float], float]] = {
    "identity": lambda v: v,
    "per_second_to_per_minute": lambda v: v * 60.0,
    "fahrenheit_to_celsius": lambda v: (v - 32.0) * 5.0 / 9.0,
    "percent_to_fraction": lambda v: v / 100.0,
    "seconds_to_milliseconds": lambda v: v * 1000.0,
}


@dataclass(frozen=True)
class FieldMap:
    target: str                  # PhysiologicalSample field
    source: str                  # provider field name
    conversion: str = "identity" # a CONVERSIONS key, or "category" with `categories`
    categories: dict | None = None
    note: str = ""


@dataclass(frozen=True)
class Mapping:
    provider: str
    version: str
    fields: tuple[FieldMap, ...]
    unmapped: dict = field(default_factory=dict)   # provider field -> why it is not mapped (documented, kept in provenance)
    quality: dict = field(default_factory=dict)    # provider field -> meaning (e.g. a worn/contact flag) used by normalization

    def validate(self) -> list[str]:
        e, seen_t, seen_s = [], set(), set()
        for m in self.fields:
            if m.target not in TARGETS:
                e.append(f"{m.target}: not a PhysiologicalSample measurement field")
            if m.conversion != "category" and m.conversion not in CONVERSIONS:
                e.append(f"{m.target}: unknown conversion {m.conversion!r}")
            if m.conversion == "category" and not m.categories:
                e.append(f"{m.target}: a category mapping needs its table")
            if m.target in seen_t or m.source in seen_s:
                e.append(f"{m.target} <- {m.source}: each target and source may appear once")
            seen_t.add(m.target); seen_s.add(m.source)
            if m.source in self.unmapped:
                e.append(f"{m.source} is listed both as mapped and as unmapped")
        return e

    def describe(self) -> list[dict]:
        return [{"target": m.target, "source": m.source, "conversion": m.conversion, **({"categories": m.categories} if m.categories else {}), **({"note": m.note} if m.note else {})}
                for m in self.fields]


def _number(v) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except ValueError:
            return None
    return None


def apply(mapping: Mapping, raw: dict) -> tuple[dict, list[dict]]:
    """Map one record's provider fields; returns (measurements for PhysiologicalSample, issues). Empty/absent -> omitted."""
    values, issues = {}, []
    for m in mapping.fields:
        if m.source not in raw:
            continue
        v = raw[m.source]
        if v is None or (isinstance(v, str) and not v.strip()):
            continue                                    # not reported: stays null, no issue
        if m.conversion == "category":
            key = str(v).strip()
            if key not in m.categories:
                issues.append({"field": m.target, "source": m.source, "raw": v, "reason": "not in the category table"})
                continue
            values[m.target] = m.categories[key]
            continue
        n = _number(v)
        if n is None or not math.isfinite(n):
            issues.append({"field": m.target, "source": m.source, "raw": v, "reason": "not a finite number"})
            continue
        converted = CONVERSIONS[m.conversion](n)
        try:   # the model's own range check; out of range is an issue, never clamped
            PhysiologicalSample.model_validate({"athlete_id": "x", "timestamp": "2026-01-01T00:00:00Z", "source": "wearable", "device_id": "x", m.target: converted})
        except Exception:  # noqa: BLE001
            issues.append({"field": m.target, "source": m.source, "raw": v, "converted": round(converted, 6), "reason": "outside the model's valid range after conversion"})
            continue
        values[m.target] = round(converted, 6)
    return values, issues
