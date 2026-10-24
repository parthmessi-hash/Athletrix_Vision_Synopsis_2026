"""Shared request-time timestamp checks for ingest endpoints."""

from datetime import datetime

from fastapi import HTTPException

from app.core import clock


def reject_future_timestamp(timestamp: datetime) -> None:
    """Raise 422 if the timestamp is further ahead of backend time than allowed."""
    error = clock.future_skew_error(timestamp)
    if error is not None:
        raise HTTPException(
            status_code=422,
            detail=[{"loc": ["body", "timestamp"], "msg": error, "type": "value_error.timestamp_in_future"}],
        )
