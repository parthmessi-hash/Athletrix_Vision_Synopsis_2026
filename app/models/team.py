from pydantic import AwareDatetime, Field

from app.models.athlete import Athlete
from app.models.common import AthletrixModel
from app.models.device import DeviceStatus
from app.models.physiological import PhysiologicalSample
from app.models.tracking import TrackingSample


class ReportedDevice(AthletrixModel):
    """A device id reported by an athlete's newest samples, with that device's latest status report if one exists.

    The association is only that the athlete's newest sample(s) named this device id: it is a reported association,
    not a verified identity or a permanent assignment. `status` is the newest report the engine holds for the id
    (null when none was ever received) and `status_age_seconds` is captured_at minus its timestamp. A report
    describes the moment it was sent: an old "connected" is not proof of a current connection, and nothing here
    infers connection or disconnection from samples arriving or not.
    """

    device_id: str = Field(..., description="Device id as reported in the athlete's newest sample(s).")
    streams: list[str] = Field(..., description="Which of the athlete's newest samples reported this id: physiological, tracking, or both.")
    status: DeviceStatus | None = Field(None, description="Latest status report received for this device id; null if none.")
    status_age_seconds: float | None = Field(None, ge=0, description="captured_at minus the status report timestamp; null if no report.")


class TeamAthleteLive(AthletrixModel):
    """One registered athlete's newest retained reading per stream, as of the team snapshot's captured instant.

    A sample is the newest retained one whose recorded timestamp is at or before `captured_at`; samples
    accepted within the ingestion clock-skew allowance but timestamped after that instant are not shown.
    Ages are `captured_at` minus the sample timestamp, in seconds, never negative. An old sample is still
    returned with its age, so a consumer can tell a stale reading from no reading at all; null means no
    retained sample at all for that stream. Nothing here judges freshness.
    """

    athlete: Athlete = Field(..., description="Registered identity (from the athlete registry).")
    physiological: PhysiologicalSample | None = Field(None, description="Newest retained physiological sample at or before captured_at; null if none.")
    tracking: TrackingSample | None = Field(None, description="Newest retained tracking sample at or before captured_at; null if none.")
    physiological_age_seconds: float | None = Field(None, ge=0, description="captured_at minus the physiological sample timestamp; null if no sample.")
    tracking_age_seconds: float | None = Field(None, ge=0, description="captured_at minus the tracking sample timestamp; null if no sample.")
    devices: list[ReportedDevice] = Field(default_factory=list, description="Device ids named by the newest samples above (deduplicated), each with its latest status report if any; empty when neither sample reports a device id.")


class TeamLive(AthletrixModel):
    """Live overview of every athlete registered with a team id, read as one snapshot.

    Membership is Athlete.team_id in the registry; there is no separate team registry, so an unknown
    team id simply has no athletes. Athletes are sorted by jersey number, then id, for a stable display.
    """

    team_id: str = Field(..., description="Team id as requested.")
    captured_at: AwareDatetime = Field(..., description="Backend time read once for this snapshot (UTC).")
    athletes: list[TeamAthleteLive] = Field(..., description="Registered athletes on the team, sorted by jersey number then id.")
