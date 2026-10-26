"""Accepted-sample ingestion: the rolling store write and, while a recording is active, the recording write, in one transaction.

Membership in a recording is decided here, at ingestion: a sample accepted while a recording is active, for one of
the recording's athletes, is written to the recording's own table in the same `BEGIN IMMEDIATE` transaction as the
rolling insert-plus-trim. Either both writes commit or neither does, so a successful POST means the rolling history
and the recording agree. The sample's own timestamp plays no part in membership (delayed samples and historical
replays are recorded as received), and a stopped recording is never written again because the active-recording
read happens inside the same serialized transaction as the stop. Validation and the future-timestamp rule are
unchanged and happen before this.
"""

from app.store.recordings import recording_store
from app.store.samples import SampleStore


def ingest(store: SampleStore, stream: str, sample) -> None:
    """Persist one validated sample: rolling store (insert or replace, trim) plus the active recording if it covers the athlete."""
    with store.db.transaction() as c:
        store.write(c, sample)
        active = recording_store.active_in(c)
        if active is not None and sample.athlete_id in active.athlete_ids:
            recording_store.write_sample(c, active.id, stream, sample)
