from pydantic import AwareDatetime, Field

from app.models.common import AthletrixModel
from app.models.physiological import PhysiologicalSample
from app.models.tracking import TrackingSample


class AthleteWindow(AthletrixModel):
    """Samples recorded for one athlete within a recent time window.

    This groups samples purely by their recorded timestamps. It does not
    correct for clock differences between devices, and it does not account
    for the lag between physical effort and physiological response. Both
    lists are limited by the per-stream history cap, so a long window may
    return fewer samples than the window could hold.
    """

    athlete_id: str = Field(..., description="Athlete the window belongs to.")
    window_start: AwareDatetime = Field(..., description="Inclusive start of the window (UTC).")
    window_end: AwareDatetime = Field(..., description="Inclusive end of the window: backend time at the request (UTC).")
    physiological_samples: list[PhysiologicalSample] = Field(..., description="Oldest to newest.")
    tracking_samples: list[TrackingSample] = Field(..., description="Oldest to newest.")
