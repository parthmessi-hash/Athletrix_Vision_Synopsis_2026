from pydantic import Field

from app.models.common import AthletrixModel


class Athlete(AthletrixModel):
    """A monitored athlete. Sport is free text so any sport can be onboarded."""

    id: str = Field(..., min_length=1, description="Unique athlete identifier.")
    name: str = Field(..., min_length=1, description="Display name.")
    jersey_number: int = Field(..., ge=0, le=999, description="Jersey/shirt number.")
    sport: str = Field(..., min_length=1, description="Sport name, e.g. 'football', 'basketball'.")
    team_id: str = Field(..., min_length=1, description="Identifier of the athlete's team.")
