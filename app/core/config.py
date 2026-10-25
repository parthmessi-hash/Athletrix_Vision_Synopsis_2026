"""Application settings, loaded from environment variables or a local .env file."""

from functools import lru_cache

from pydantic import Field
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

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
