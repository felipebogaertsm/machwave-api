from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Deployment environment. "local" spawns the simulation worker as an
    # in-process subprocess; "prod" submits a Cloud Run Job execution.
    env: Literal["local", "prod"] = "prod"

    # GCS
    gcs_bucket_name: str

    # Firebase
    firebase_project_id: str

    # GCP / Pub/Sub
    gcp_project_id: str
    pubsub_topic: str = "machwave-simulations"
    pubsub_subscription: str = "machwave-simulations-push"

    # CORS — accept a comma-separated string and split it
    cors_origins: str = "http://localhost:3000"

    # Default per-user caps. Admins have no caps.
    default_motor_limit: int = 10
    default_simulation_limit: int = 10
    default_monthly_token_limit: int = 10_000

    # Maximum number of teams a non-admin user can belong to (owned + joined).
    # Admins are unlimited.
    default_team_membership_limit: int = 5

    # Default per-team caps applied when a TeamAccount is first created.
    default_team_motor_limit: int = 50
    default_team_simulation_limit: int = 50
    default_team_monthly_token_limit: int = 100_000

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
