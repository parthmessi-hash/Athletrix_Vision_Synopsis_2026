"""Availability context: what is retained for a stream against what one request selected from it. Pure functions.

The retained facts come from the store's `retention()` read; the selected facts come from the samples the request
already chose, so the metadata can never disagree with the records it accompanies when both are read under one
database read. Nothing here estimates expected counts, rates, coverage or the reason data is absent.
"""

from collections.abc import Sequence

from app.models.availability import StreamAvailability
from app.models.recording import SessionStreamAvailability
from app.store.samples import Retention


def session_stream_availability(recorded: Retention, selected: Sequence) -> SessionStreamAvailability:
    """Describe a recording's rows for one stream (`recorded`) against `selected`, the request's samples; no cap exists."""
    stamps = sorted(s.timestamp for s in selected)
    return SessionStreamAvailability(
        recorded_count=recorded.count, recorded_earliest=recorded.earliest, recorded_latest=recorded.latest,
        selected_count=len(stamps), selected_first=stamps[0] if stamps else None, selected_last=stamps[-1] if stamps else None,
    )


def stream_availability(retention: Retention, max_samples: int, selected: Sequence) -> StreamAvailability:
    """Describe `retention` (rows stored now, cap `max_samples`) against `selected` (the request's samples, any order)."""
    stamps = sorted(s.timestamp for s in selected)
    return StreamAvailability(
        max_samples=max_samples,
        retained_count=retention.count,
        retained_earliest=retention.earliest,
        retained_latest=retention.latest,
        at_cap=retention.count >= max_samples,
        selected_count=len(stamps),
        selected_first=stamps[0] if stamps else None,
        selected_last=stamps[-1] if stamps else None,
    )
