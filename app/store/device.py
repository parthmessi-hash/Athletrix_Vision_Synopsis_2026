"""Latest DeviceStatus per device (in-memory)."""

from app.models import DeviceStatus
from app.store.latest import LatestSampleStore


def LatestDeviceStatusStore() -> LatestSampleStore[DeviceStatus]:
    return LatestSampleStore(key=lambda s: s.device_id)


latest_device_status_store = LatestDeviceStatusStore()
