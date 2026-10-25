from pydantic import AwareDatetime, Field

from app.models.common import AthletrixModel, SampleSource


class TrackingSample(AthletrixModel):
    """One point-in-time position/motion reading for an athlete.

    Units:
      - timestamp:           ISO 8601 with timezone (UTC recommended)
      - x_position:          metres, in the playing-area coordinate frame
      - y_position:          metres, in the playing-area coordinate frame
      - speed_mps:           metres per second (magnitude, never negative)
      - acceleration_mps2:   metres per second squared (negative = decelerating)
      - tracking_confidence: unitless, normalised 0.0 (lost) to 1.0 (certain)

    The origin and orientation of the coordinate frame are defined per venue
    and are not part of this model.

    `source` and `device_id` are optional and backward compatible: payloads
    and stored records without them stay valid and mean "not reported".
    They are supplied metadata (a generic category and an identifier chosen
    by the sender), never inferred from file names, values or delivery path,
    and never a claim about authenticity or sensor accuracy.
    """

    athlete_id: str = Field(..., min_length=1, description="Athlete this sample belongs to.")
    timestamp: AwareDatetime = Field(..., description="When the reading was taken (timezone required).")
    x_position: float = Field(..., description="X position, metres.")
    y_position: float = Field(..., description="Y position, metres.")
    speed_mps: float = Field(..., ge=0, description="Speed, metres per second.")
    acceleration_mps2: float = Field(..., description="Acceleration, metres per second squared.")
    tracking_confidence: float = Field(..., ge=0.0, le=1.0, description="Normalised confidence, 0.0 to 1.0.")
    source: SampleSource | None = Field(None, description="Generic source category of the reading, if reported.")
    device_id: str | None = Field(None, min_length=1, description="Identifier of the reporting device, if reported.")
