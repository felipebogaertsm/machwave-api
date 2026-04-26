from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Deployment environment. "local" spawns the simulation worker as an
    # in-process subprocess; "prod" submits a Cloud Run Job execution.
    env: Literal["local", "prod"] = "prod"

    # GCS
    gcs_bucket_name: str

    # Firebase
    firebase_project_id: str

    # GCP / Cloud Run Jobs
    gcp_project_id: str
    cloud_run_job_name: str = "machwave-worker"
    cloud_run_job_region: str = "us-central1"

    # CORS — accept a comma-separated string and split it
    cors_origins: str = "http://localhost:3000"

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
