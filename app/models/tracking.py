from pydantic import AwareDatetime, Field

from app.models.common import AthletrixModel


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
    """

    athlete_id: str = Field(..., min_length=1, description="Athlete this sample belongs to.")
    timestamp: AwareDatetime = Field(..., description="When the reading was taken (timezone required).")
    x_position: float = Field(..., description="X position, metres.")
    y_position: float = Field(..., description="Y position, metres.")
    speed_mps: float = Field(..., ge=0, description="Speed, metres per second.")
    acceleration_mps2: float = Field(..., description="Acceleration, metres per second squared.")
    tracking_confidence: float = Field(..., ge=0.0, le=1.0, description="Normalised confidence, 0.0 to 1.0.")
