from pydantic import AwareDatetime, Field

from app.models.common import AthletrixModel, Posture, SampleSource


class PhysiologicalSample(AthletrixModel):
    """One point-in-time physiological reading for an athlete.

    Every measurement is optional because different devices report different
    subsets. A sample with no measurements is still valid (e.g. a heartbeat
    from a device that is connected but has no sensor contact).

    Units:
      - timestamp:          ISO 8601 with timezone (UTC recommended)
      - heart_rate_bpm:     beats per minute
      - breathing_rate_bpm: breaths per minute
      - hrv_rmssd_ms:       milliseconds (root mean square of successive RR differences)
      - hrv_sdnn_ms:        milliseconds (standard deviation of NN intervals)
      - activity_level:     unitless, normalised 0.0 (rest) to 1.0 (max effort)
      - skin_temperature_c: degrees Celsius
      - spo2_percent:       blood oxygen saturation, percent
      - signal_quality:     unitless, normalised 0.0 (unusable) to 1.0 (excellent)
    """

    athlete_id: str = Field(..., min_length=1, description="Athlete this sample belongs to.")
    timestamp: AwareDatetime = Field(..., description="When the reading was taken (timezone required).")
    source: SampleSource = Field(..., description="Generic source category of the reading.")
    device_id: str = Field(..., min_length=1, description="Identifier of the reporting device.")

    heart_rate_bpm: float | None = Field(None, ge=20, le=260, description="Heart rate, beats per minute.")
    breathing_rate_bpm: float | None = Field(None, ge=0, le=100, description="Breathing rate, breaths per minute.")
    hrv_rmssd_ms: float | None = Field(None, ge=0, le=1000, description="HRV RMSSD, milliseconds.")
    hrv_sdnn_ms: float | None = Field(None, ge=0, le=1000, description="HRV SDNN, milliseconds.")
    activity_level: float | None = Field(None, ge=0.0, le=1.0, description="Normalised activity level, 0.0 to 1.0.")
    posture: Posture | None = Field(None, description="Coarse posture classification.")
    skin_temperature_c: float | None = Field(None, ge=20, le=45, description="Skin temperature, degrees Celsius.")
    spo2_percent: float | None = Field(None, ge=0, le=100, description="Blood oxygen saturation, percent.")
    signal_quality: float | None = Field(None, ge=0.0, le=1.0, description="Normalised signal quality, 0.0 to 1.0.")
