"""Application settings, loaded from environment variables or a local .env file."""

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Athletrix Engine"
    app_version: str = "0.1.0"
    host: str = "127.0.0.1"
    port: int = 8000
    # Origins allowed to call the API. Defaults cover common frontend dev servers.
    cors_origins: str = "http://localhost:3000,http://localhost:5173"
    # Local SQLite file holding athletes, device status and bounded sample histories.
    # Relative paths are resolved from the working directory the backend is started in.
    database_path: str = "data/athletrix.db"
    # Distance estimate rules (adjustable engineering defaults, not validated sport-specific standards):
    # consecutive tracking samples further apart than this are not bridged, and both ends of an interval
    # must reach this tracking_confidence for it to count.
    distance_max_gap_seconds: float = Field(3.0, gt=0, le=60)
    distance_min_confidence: float = Field(0.5, ge=0, le=1)
    # Speed at or above which time and distance count as "high-speed" (m/s). 5.0 is a demonstration
    # default, not a validated standard for every sport or athlete.
    high_speed_threshold_mps: float = Field(5.0, gt=0, allow_inf_nan=False)
    # Retention: newest samples kept per athlete, separately per stream (positive integers; default 300 each).
    # A count, not a time span: 300 tracking samples at 10 Hz is 30 s, 3000 is about five minutes, and at 1 Hz
    # 300 samples is five minutes. Applied by the insert-plus-trim transaction of each accepted sample; nothing
    # stored is pruned or rewritten at startup, so a reduced cap takes effect for an athlete's stream when its
    # next sample is accepted, and a raised cap never brings trimmed rows back.
    physiological_retention_samples: int = Field(300, gt=0)
    tracking_retention_samples: int = Field(300, gt=0)

    @field_validator("physiological_retention_samples", "tracking_retention_samples", mode="before")
    @classmethod
    def _retention_is_a_plain_integer(cls, v):
        if isinstance(v, bool):
            raise ValueError("a retention cap must be a positive integer, not a boolean")
        return v

    def retention_cap(self, stream: str) -> int:
        """Newest rows kept per athlete for `stream` ("physiological" or "tracking")."""
        if stream == "physiological":
            return self.physiological_retention_samples
        if stream == "tracking":
            return self.tracking_retention_samples
        raise ValueError(f"unknown stream {stream!r}")

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
