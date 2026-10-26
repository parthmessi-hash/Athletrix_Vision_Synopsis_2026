"""History retention policy shared by the sample stores (see app/store/samples.py).

Each athlete keeps at most a configured number of their newest samples per stream, persisted in the local SQLite
database: `PHYSIOLOGICAL_RETENTION_SAMPLES` and `TRACKING_RETENTION_SAMPLES` (positive integers, default 300 each,
read through the application settings). The two caps are independent. Each is a sample count, not a time window:
300 tracking samples at 10 Hz is 30 s, 3000 is about five minutes, and at 1 Hz 300 samples is five minutes; a count
never guarantees a time span. Nothing stored is pruned or rewritten at startup: a cap is applied by the
insert-plus-trim transaction of each accepted sample, so a reduced cap takes effect for an athlete's stream when its
next sample is accepted, and a raised cap never brings trimmed rows back. This still does not store full matches or
seasons.
"""

from app.core.config import get_settings

# Default newest samples kept per athlete per stream when nothing is configured. A count, not a duration.
DEFAULT_RETENTION_SAMPLES = 300
# Kept for callers that still import the old single-cap name; it is the default, not necessarily the configured cap.
MAX_HISTORY_SAMPLES = DEFAULT_RETENTION_SAMPLES

STREAMS = ("physiological", "tracking")


def retention_cap(stream: str) -> int:
    """The configured cap for one stream, read from the settings in force."""
    return get_settings().retention_cap(stream)


def retention_caps() -> dict[str, int]:
    """Both configured caps, keyed by stream."""
    return {s: retention_cap(s) for s in STREAMS}
