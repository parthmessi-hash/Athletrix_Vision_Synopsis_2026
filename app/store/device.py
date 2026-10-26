"""Latest DeviceStatus per device, persisted in SQLite.

Newest timestamp wins; an equal instant takes the most recent arrival; an older status is accepted by
the API but does not replace a newer stored one. The comparison happens inside the UPDATE, so it is
atomic with the write.
"""

from __future__ import annotations

from app.db import Database, get_database, to_utc_iso
from app.models import DeviceStatus


class DeviceStatusStore:
    def __init__(self, db: Database | None = None) -> None:
        self._db = db

    @property
    def db(self) -> Database:
        return self._db or get_database()

    def upsert(self, status: DeviceStatus) -> DeviceStatus:
        """Store the status if it is at least as new as the stored one; return whichever is now stored."""
        with self.db.transaction() as c:
            c.execute(
                "INSERT INTO device_status (device_id, ts_utc, payload) VALUES (?, ?, ?) "
                "ON CONFLICT(device_id) DO UPDATE SET ts_utc = excluded.ts_utc, payload = excluded.payload "
                "WHERE excluded.ts_utc >= device_status.ts_utc",
                (status.device_id, to_utc_iso(status.timestamp), status.model_dump_json()),
            )
        stored = self.get(status.device_id)
        assert stored is not None
        return stored

    def get(self, device_id: str) -> DeviceStatus | None:
        with self.db.read() as c:
            row = c.execute("SELECT payload FROM device_status WHERE device_id = ?", (device_id,)).fetchone()
        return DeviceStatus.model_validate_json(row["payload"]) if row else None

    def get_many(self, device_ids: list[str]) -> dict[str, DeviceStatus]:
        """Latest status for each of the given device ids that has one, in one query (ids without a status are absent)."""
        ids = sorted({d for d in device_ids if d})
        if not ids:
            return {}
        with self.db.read() as c:
            rows = c.execute(f"SELECT device_id, payload FROM device_status WHERE device_id IN ({','.join('?' * len(ids))})", ids).fetchall()
        return {r["device_id"]: DeviceStatus.model_validate_json(r["payload"]) for r in rows}

    def clear(self) -> None:
        with self.db.transaction() as c:
            c.execute("DELETE FROM device_status")


def LatestDeviceStatusStore(db: Database | None = None) -> DeviceStatusStore:
    return DeviceStatusStore(db)


latest_device_status_store = DeviceStatusStore()
