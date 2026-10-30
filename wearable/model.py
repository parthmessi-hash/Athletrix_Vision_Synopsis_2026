"""What a provider reader hands to normalization: raw records exactly as the provider stated them."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol

RecordKind = Literal["data", "connected", "disconnected"]


@dataclass(frozen=True)
class RawRecord:
    """One provider record. `observed` is the provider's own time value as given (text or number) and `received_at` the
    reader's UTC receipt time when known (None for a file without receipt information). `fields` are the provider's
    values under the provider's own names and units; `line` locates the record in its source for error messages."""

    provider: str
    device: str
    kind: RecordKind
    observed: object
    received_at: datetime | None
    fields: dict = field(default_factory=dict)
    line: int = 0


class ProviderReader(Protocol):
    """A provider reader: parses one provider's output, reports format problems per line, never maps or assigns."""

    provider: str

    def read(self) -> list[RawRecord]: ...

    @property
    def problems(self) -> list[str]: ...
