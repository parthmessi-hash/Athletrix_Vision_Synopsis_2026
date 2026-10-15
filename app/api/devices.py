from fastapi import APIRouter

from app.models import DeviceStatus

router = APIRouter(prefix="/devices", tags=["devices"])


@router.post("/status", response_model=DeviceStatus)
def ingest_device_status(status: DeviceStatus) -> DeviceStatus:
    """Validate a device status report and echo it back. No persistence yet."""
    return status
