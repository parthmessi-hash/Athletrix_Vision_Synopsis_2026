from pydantic import AwareDatetime, Field

from app.models.common import AthletrixModel


class StreamRetention(AthletrixModel):
    """What the engine currently retains for one athlete in one stream.

    `sample_count` is the number of rows still stored (at most the per-stream
    cap). The timestamps are the recorded timestamps of the oldest and newest
    retained rows, expressed in UTC. They bound the retained records only:
    they do not show that every instant between them was sampled, and they say
    nothing about records that were never sent or were removed by the
    newest-N retention rule. An empty stream has count 0 and null timestamps.
    """

    sample_count: int = Field(..., ge=0, description="Rows currently retained for this athlete and stream.")
    earliest_timestamp: AwareDatetime | None = Field(None, description="Recorded timestamp of the oldest retained row (UTC); null if none.")
    latest_timestamp: AwareDatetime | None = Field(None, description="Recorded timestamp of the newest retained row (UTC); null if none.")


class RetainedData(AthletrixModel):
    """Counts and recorded time ranges of the samples currently retained for one athlete.

    Describes the records in local storage at the moment of the request, per
    stream. It is not a session list, a recording archive or a coverage report:
    the ranges can differ between streams, nothing is inferred about gaps or
    evicted records, and a count of 0 does not establish whether data was never
    collected or has since been removed. Registration is not required for
    ingestion, so an unknown athlete id simply reports empty streams.
    """

    athlete_id: str = Field(..., description="Athlete the counts belong to.")
    physiological: StreamRetention = Field(..., description="Retained physiological samples.")
    tracking: StreamRetention = Field(..., description="Retained tracking samples.")
    max_samples_per_stream: int = Field(..., gt=0, description="The engine's retention cap: newest samples kept per athlete per stream.")
