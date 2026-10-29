"""The configuration snapshot captured when a recording starts (see app/models/configuration.py).

Built from the settings object in force and the engine's own constants only, never from the environment at large,
so it cannot pick up secrets, paths or machine details. The code revision is whatever CODE_REVISION was supplied at
startup (unknown when unset); nothing here runs git.
"""

from datetime import datetime

from app.core.clock import MAX_FUTURE_TIMESTAMP_SKEW
from app.core.effort import AFTER_SECONDS, BEFORE_SECONDS, MAX_EFFORT_SECONDS, MIN_EFFORT_SECONDS
from app.models.configuration import AdapterConfiguration, OperatorSupplied, RecordingConfiguration


def capture_configuration(settings, captured_at: datetime, adapter: AdapterConfiguration | None, package_format_version: int) -> RecordingConfiguration:
    supplied = None
    if adapter is not None and (adapter.note or adapter.metadata):
        supplied = OperatorSupplied(note=adapter.note, metadata=adapter.metadata)
    return RecordingConfiguration(
        captured_at=captured_at,
        engine={"application": settings.app_name, "version": settings.app_version, "code_revision": settings.code_revision, "package_format_version": package_format_version},
        movement={"distance_max_gap_seconds": settings.distance_max_gap_seconds, "distance_min_confidence": settings.distance_min_confidence, "high_speed_threshold_mps": settings.high_speed_threshold_mps},
        retention={"physiological_retention_samples": settings.physiological_retention_samples, "tracking_retention_samples": settings.tracking_retention_samples},
        timing={"max_future_timestamp_skew_seconds": MAX_FUTURE_TIMESTAMP_SKEW.total_seconds()},
        effort_review={"min_effort_seconds": MIN_EFFORT_SECONDS, "max_effort_seconds": MAX_EFFORT_SECONDS, "before_seconds": BEFORE_SECONDS, "after_seconds": AFTER_SECONDS},
        operator_supplied=supplied,
    )
