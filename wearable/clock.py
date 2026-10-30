"""How a provider's observation time becomes a UTC sample timestamp, with its stated uncertainty.

Three explicit bases (never guessed):
  device_utc      the provider states the time in UTC (or with an offset); timestamp = that time. `uncertainty_s` is the
                  operator's statement of how far the device clock may be off (unknown unless measured).
  device_offset   the provider gives device-clock times; timestamp = device time + `offset_s`, where offset_s was
                  measured (for example by noting a device time and a UTC time together) and is stated with its
                  uncertainty.
  receipt_minus   the provider gives no usable observation time; timestamp = receipt time - `latency_s`. Only allowed
                  when every record has a receipt time; the uncertainty must cover the latency's variation.
Receipt times are always kept separately in the provenance record: an observation time is when the body was measured,
a receipt time is when the reader got the record, and the engine adds its own receipt time when it accepts a sample.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

BASES = ("device_utc", "device_offset", "receipt_minus")


@dataclass(frozen=True)
class ClockMapping:
    basis: str = "device_utc"
    offset_s: float = 0.0
    latency_s: float = 0.0
    uncertainty_s: float | None = None     # None: not measured (stated as unknown in provenance)
    note: str = ""

    def validate(self) -> list[str]:
        e = []
        if self.basis not in BASES:
            e.append(f"basis must be one of {', '.join(BASES)}")
        if self.uncertainty_s is not None and self.uncertainty_s < 0:
            e.append("uncertainty_s must be >= 0 or null (unknown)")
        if self.basis != "device_offset" and self.offset_s:
            e.append("offset_s applies only to basis device_offset")
        if self.basis != "receipt_minus" and self.latency_s:
            e.append("latency_s applies only to basis receipt_minus")
        return e

    def to_utc(self, device_time: datetime | None, received_at: datetime | None) -> datetime:
        if self.basis == "receipt_minus":
            if received_at is None:
                raise ValueError("basis receipt_minus needs a receipt time for every record")
            return (received_at - timedelta(seconds=self.latency_s)).astimezone(UTC)
        if device_time is None:
            raise ValueError("this record has no observation time")
        if self.basis == "device_offset":
            return (device_time + timedelta(seconds=self.offset_s)).astimezone(UTC)
        return device_time.astimezone(UTC)

    def describe(self) -> dict:
        return {**asdict(self), "uncertainty_s": self.uncertainty_s if self.uncertainty_s is not None else "unknown (not measured)"}
