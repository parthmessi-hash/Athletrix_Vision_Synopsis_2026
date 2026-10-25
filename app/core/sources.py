"""Source breakdown of a list of samples. Pure function, shared by the window and summary responses."""

from collections import Counter
from collections.abc import Iterable

from app.models.common import SampleSource
from app.models.sources import SourceBreakdown


def source_breakdown(samples: Iterable[object]) -> SourceBreakdown:
    """Count the `source` of every sample given (a sample with none, or with the attribute missing, is not_reported).

    Every sample counts once, whatever its measurements hold (a physiological sample with a null heart rate still
    counts), so the result always adds up to the number of samples passed in.
    """
    reported: Counter[SampleSource] = Counter()
    not_reported = 0
    for s in samples:
        src = getattr(s, "source", None)
        if src is None:
            not_reported += 1
        else:
            reported[SampleSource(src)] += 1
    return SourceBreakdown(reported=dict(sorted(reported.items(), key=lambda kv: kv[0].value)), not_reported=not_reported)
