from pydantic import AwareDatetime, Field

from app.models.common import AthletrixModel


class DeviceStatus(AthletrixModel):
    """Health of a data-producing device (wearable, camera, etc.).

    Units:
      - timestamp:       ISO 8601 with timezone (UTC recommended)
      - battery_percent: percent, 0 to 100
      - signal_quality:  unitless, normalised 0.0 (unusable) to 1.0 (excellent)
    """

    device_id: str = Field(..., min_length=1, description="Identifier of the device.")
    timestamp: AwareDatetime = Field(..., description="When the status was reported (timezone required).")
    connected: bool = Field(..., description="Whether the device is currently connected.")
    battery_percent: float | None = Field(None, ge=0, le=100, description="Battery level, percent.")
    signal_quality: float | None = Field(None, ge=0.0, le=1.0, description="Normalised signal quality, 0.0 to 1.0.")
