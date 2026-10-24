from fastapi import APIRouter, HTTPException

from app.api.timestamps import reject_future_timestamp
from app.models import DeviceStatus
from app.store.device import latest_device_status_store

router = APIRouter(prefix="/devices", tags=["devices"])


@router.post("/status", response_model=DeviceStatus)
def ingest_device_status(status: DeviceStatus) -> DeviceStatus:
    """Validate a device status report, record it as the device's latest if newest, and echo it back."""
    reject_future_timestamp(status.timestamp)
    latest_device_status_store.upsert(status)
    return status


@router.get("/{device_id}/status/latest", response_model=DeviceStatus)
def get_latest_device_status(device_id: str) -> DeviceStatus:
    """Return the most recent status report received for a device."""
    status = latest_device_status_store.get(device_id)
    if status is None:
        raise HTTPException(status_code=404, detail=f"No status for device '{device_id}'")
    return status
