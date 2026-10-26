from pydantic import AwareDatetime, Field

from app.models.common import AthletrixModel


class StreamAvailability(AthletrixModel):
    """What the engine retains for one stream, against what a request selected from it.

    `retained_*` describe the rows stored for the athlete in this stream at the moment of the request (the same facts
    as the retained-data endpoint) and `max_samples` is the stream's configured cap. `selected_*` describe the samples
    the request actually chose from those rows for its period: the count is exactly the number of samples in the
    accompanying result, and the first/last timestamps are their recorded bounds.

    What this does not say: a selection whose first and last timestamps reach the period's bounds is not thereby
    complete, since nothing here counts gaps inside it; no expected count, sampling rate or coverage fraction is
    implied; and retained history that starts after the period starts (or ends before it ends) does not show why.
    `at_cap` is the engine's only evidence about removal: when the stream holds as many rows as its cap, any older
    sample that was received has been trimmed. Below the cap, the engine cannot tell whether earlier data was never
    received or was removed under an earlier cap. A cap is a row count, not a time span.
    """

    max_samples: int = Field(..., gt=0, description="This stream's configured retention cap: newest rows kept per athlete.")
    retained_count: int = Field(..., ge=0, description="Rows retained for the athlete in this stream right now.")
    retained_earliest: AwareDatetime | None = Field(None, description="Recorded timestamp of the oldest retained row (UTC); null if none.")
    retained_latest: AwareDatetime | None = Field(None, description="Recorded timestamp of the newest retained row (UTC); null if none.")
    at_cap: bool = Field(..., description="retained_count >= max_samples: any older sample that was received is no longer retained.")
    selected_count: int = Field(..., ge=0, description="Samples the request selected from the retained rows for its period.")
    selected_first: AwareDatetime | None = Field(None, description="Recorded timestamp of the oldest selected sample; null if none.")
    selected_last: AwareDatetime | None = Field(None, description="Recorded timestamp of the newest selected sample; null if none.")


class WindowAvailability(AthletrixModel):
    """Per-stream availability for a window or summary request, consistent with the samples it returned."""

    physiological: StreamAvailability
    tracking: StreamAvailability


class HeartRateAvailability(AthletrixModel):
    """Physiological availability for each of the three effort-review periods; the retained facts are the same in all three."""

    before: StreamAvailability
    during: StreamAvailability
    after: StreamAvailability


class EffortAvailability(AthletrixModel):
    """Availability context for an effort review: tracking over the effort, physiological per period. Boundary rules are unchanged."""

    movement: StreamAvailability
    heart_rate: HeartRateAvailability
