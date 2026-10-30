"""Zephyr adapter boundary: DELIBERATELY NOT IMPLEMENTED.

Zephyr is the team's preferred wearable. The supported path described by the vendor is
    BioModule -> ECHO -> OmniSense -> OmniSense Live API (local) -> this adapter -> wearable/normalize.py -> engine.
Athletrix never talks to ECHO. Nothing here communicates with any Zephyr software: no API, SDK, endpoint, binding or
packet format is assumed. The vendor has sent an API document under a confidentiality notice and has said sample data
will follow; until the team holds that documentation with permission to use it AND real sample payloads, writing a
reader would mean inventing one. The precise list of what is needed is docs/zephyr-integration-checklist.md.

When those exist, the work is: a reader that produces wearable.model.RawRecord from the Live API (connection and
reconnection as "connected"/"disconnected" records, no samples invented for gaps), a wearable.mapping.Mapping built
only from documented fields and units, a parse_time from the documented timestamp semantics, and fixtures made from
the real sample payloads. Rules already fixed by the foundation apply: a calculated HRV value is not beat intervals
and goes into hrv_rmssd_ms or hrv_sdnn_ms only if the documentation says which statistic it is; an estimated core
temperature is never a skin temperature; unsupported measurements are left unmapped.
"""

from __future__ import annotations

MISSING = (
    "permission from the vendor to use the API documentation for this project (it carries a confidentiality notice)",
    "real Live API sample payloads (the vendor said they would follow; none are in the project)",
    "the transport/client the vendor supports for a third-party application on the OmniSense computer, and whether it can be used from Python or needs a vendor-supported bridge",
    "units, ranges and missing-value conventions of every field Athletrix would map",
    "timestamp semantics: device or OmniSense clock, time zone, resolution, and how it relates to receipt time",
    "which HRV statistic the live value is (RMSSD or SDNN, window length, unit)",
    "posture and activity definitions, and the meaning of any confidence, reliability or worn indicators",
    "connection, reconnection and battery/signal reporting behaviour",
    "licensing, subscription and seat terms for the evaluation and for a product",
)


class ZephyrOmniSenseReader:
    """Placeholder that refuses to run, naming exactly what is missing."""

    provider = "zephyr-omnisense-live"

    def __init__(self, *_, **__) -> None:
        raise NotImplementedError("The Zephyr reader is not implemented: no permitted documentation and sample payloads yet. Missing: " + "; ".join(MISSING))
