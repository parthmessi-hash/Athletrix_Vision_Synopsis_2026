"""Backend clock and timestamp acceptance rules.

Kept in one place so tests can substitute a controlled clock and so the
tolerance below has a single authoritative definition.
"""

from datetime import UTC, datetime, timedelta

# How far ahead of the backend's current UTC time a sample timestamp may be.
# This is an initial clock-skew allowance for devices and simulators whose
# clocks run slightly fast; it is not a tuned value. Samples further in the
# future than this are rejected, because the latest-sample stores keep the
# newest timestamp and a far-future sample would block all later real ones.
MAX_FUTURE_TIMESTAMP_SKEW = timedelta(seconds=5)


def utc_now() -> datetime:
    """Current backend time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def receipt_now() -> datetime:
    """Backend UTC wall clock read for a request's receipt time (separate from utc_now so a request's own captured
    instant is still read exactly once, and tests can control each independently)."""
    return datetime.now(UTC)


def future_skew_error(timestamp: datetime) -> str | None:
    """Return an error message if `timestamp` is too far ahead of now, else None."""
    now = utc_now()
    ahead = timestamp - now
    if ahead > MAX_FUTURE_TIMESTAMP_SKEW:
        return (
            f"timestamp {timestamp.isoformat()} is {ahead.total_seconds():.3f}s ahead of backend time "
            f"{now.isoformat()}; at most {MAX_FUTURE_TIMESTAMP_SKEW.total_seconds():g}s ahead is allowed"
        )
    return None
