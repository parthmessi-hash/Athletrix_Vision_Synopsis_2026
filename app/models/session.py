"""A local session plan: who plays, which inputs are intended, and the stated match constraints (schema version 1).

The plan is not a second configuration system. Starting a session turns it into what the engine already stores:
registered athletes, one recording (with the plan's name, SHA-256 and selected sources in the operator-supplied
start metadata), roster entries carrying each athlete's `inputs`, and one team constraint entry. Real values are typed
by the team; the template ships blank. `fictional: true` marks a demonstration plan and is carried into the recording.
"""

from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from app.models.common import AthletrixModel
from app.models.roster import InputPlan, Named


class PlannedAthlete(AthletrixModel):
    id: str = Field(..., min_length=1, max_length=128)
    name: str = Field(..., min_length=1, max_length=120)
    jersey_number: int = Field(..., ge=0, le=999)
    status: Literal["on_field", "bench"]
    current_role: str | None = Field(None, min_length=1, max_length=80)
    able_roles: list[str] = Field(default_factory=list, max_length=10)
    inputs: InputPlan = Field(default_factory=InputPlan)
    wearable_device_id: str | None = Field(None, min_length=1, max_length=128, description="The strap assigned to this athlete, if any (stated by the operator).")


class PlannedSource(AthletrixModel):
    selected: bool
    note: str | None = Field(None, max_length=200)


class CameraSource(PlannedSource):
    calibration_path: str | None = Field(None, max_length=500, description="Local calibration file (checked by the session tool, never uploaded).")
    time_mapping_path: str | None = Field(None, max_length=500)
    clip_or_camera: dict | None = Field(None, description="Resolution, lens/zoom note and camera id the calibration must match, e.g. {\"width\": 1920, \"height\": 1080, \"camera_id\": \"cam-1\"}.")


class Sources(AthletrixModel):
    wearable: PlannedSource
    camera: CameraSource


class Constraints(AthletrixModel):
    substitutions_remaining: int | None = Field(None, ge=0, le=20)
    no_substitution_before: AwareDatetime | None = None
    keep_on_field: list[Named] = Field(default_factory=list)
    do_not_bring_on: list[Named] = Field(default_factory=list)


class SessionPlan(AthletrixModel):
    format: Literal["athletrix-session-plan"]
    schema_version: Literal[1]
    name: str = Field(..., min_length=1, max_length=120)
    fictional: bool
    team_id: str = Field(..., min_length=1, max_length=128)
    sport: str = Field("soccer", min_length=1, max_length=40)
    athletes: list[PlannedAthlete] = Field(..., min_length=1, max_length=40)
    sources: Sources
    constraints: Constraints = Field(default_factory=Constraints)
    min_free_storage_mb: float = Field(500, ge=0)

    @model_validator(mode="after")
    def _consistent(self) -> "SessionPlan":
        ids = [a.id for a in self.athletes]
        if len(set(ids)) != len(ids):
            raise ValueError("athlete ids must be distinct")
        numbers = [a.jersey_number for a in self.athletes]
        if len(set(numbers)) != len(numbers):
            raise ValueError("jersey numbers must be distinct")
        devices = [a.wearable_device_id for a in self.athletes if a.wearable_device_id]
        if len(set(devices)) != len(devices):
            raise ValueError("a wearable device is assigned to two athletes")
        for a in self.athletes:
            if a.status != "on_field" and a.current_role:
                raise ValueError(f"{a.id}: current_role is only for an athlete on the field")
            if not self.sources.wearable.selected and a.inputs.physiology == "expected":
                raise ValueError(f"{a.id}: physiology is expected but the wearable source is not selected")
            if not self.sources.camera.selected and a.inputs.camera == "expected":
                raise ValueError(f"{a.id}: camera is expected but the camera source is not selected")
        for n in self.constraints.keep_on_field + self.constraints.do_not_bring_on:
            if n.athlete_id not in ids:
                raise ValueError(f"constraint names {n.athlete_id}, who is not in the plan")
        return self
