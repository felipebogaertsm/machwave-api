"""Tests for ``app.config.Settings``."""

from __future__ import annotations

import pytest

from app.config import Settings, get_settings


class TestSettings:
    def test_required_fields_load_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FIREBASE_PROJECT_ID", "fbp")
        monkeypatch.setenv("GCS_BUCKET_NAME", "bucket")
        monkeypatch.setenv("GCP_PROJECT_ID", "gcp")
        monkeypatch.delenv("ENV", raising=False)

        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert settings.firebase_project_id == "fbp"
        assert settings.gcs_bucket_name == "bucket"
        assert settings.gcp_project_id == "gcp"
        # Defaults
        assert settings.env == "prod"
        assert settings.pubsub_topic == "machwave-simulations"
        assert settings.pubsub_subscription == "machwave-simulations-push"

    def test_invalid_env_value_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pydantic import ValidationError

        monkeypatch.setenv("ENV", "staging")
        with pytest.raises(ValidationError):
            Settings(_env_file=None)  # type: ignore[call-arg]

    def test_env_var_lookup_is_case_insensitive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``case_sensitive=False`` — lowercase env vars must also resolve."""
        monkeypatch.setenv("firebase_project_id", "lower-fb")
        monkeypatch.setenv("gcs_bucket_name", "lower-bucket")
        monkeypatch.setenv("gcp_project_id", "lower-gcp")
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert settings.firebase_project_id == "lower-fb"


class TestCorsOriginsList:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("http://localhost:3000", ["http://localhost:3000"]),
            (
                "http://a.com, http://b.com ,http://c.com",
                ["http://a.com", "http://b.com", "http://c.com"],
            ),
            ("", []),  # empty string → empty list
            (",,,", []),  # only separators → empty list
            ("  http://only.com  ", ["http://only.com"]),  # whitespace stripped
        ],
    )
    def test_parses_comma_separated_string(
        self, monkeypatch: pytest.MonkeyPatch, raw: str, expected: list[str]
    ) -> None:
        monkeypatch.setenv("FIREBASE_PROJECT_ID", "fbp")
        monkeypatch.setenv("GCS_BUCKET_NAME", "bucket")
        monkeypatch.setenv("GCP_PROJECT_ID", "gcp")
        monkeypatch.setenv("CORS_ORIGINS", raw)
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert settings.cors_origins_list == expected


class TestGetSettings:
    def test_caches_instance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        get_settings.cache_clear()
        a = get_settings()
        b = get_settings()
        assert a is b
