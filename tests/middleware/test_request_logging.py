"""Tests for ``app.middleware.request_logging.LoggingMiddleware``."""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.middleware.request_logging import LoggingMiddleware


@pytest.fixture()
def app() -> FastAPI:
    application = FastAPI()
    application.add_middleware(LoggingMiddleware)

    @application.get("/ok")
    async def ok() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/boom")
    async def boom() -> None:
        raise HTTPException(status_code=400, detail="bad request")

    return application


class TestLoggingMiddleware:
    def test_success_logs_at_info(self, app: FastAPI, caplog: pytest.LogCaptureFixture) -> None:
        client = TestClient(app)
        with caplog.at_level(logging.INFO, logger="app.middleware.request_logging"):
            assert client.get("/ok").status_code == 200

        # One log line, INFO, with method + path + status + duration.
        records = [r for r in caplog.records if r.name == "app.middleware.request_logging"]
        assert len(records) == 1
        record = records[0]
        assert record.levelno == logging.INFO
        message = record.getMessage()
        assert "GET" in message
        assert "/ok" in message
        assert "200" in message
        assert "ms" in message

    def test_error_response_logs_at_error(
        self, app: FastAPI, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = TestClient(app)
        with caplog.at_level(logging.INFO, logger="app.middleware.request_logging"):
            assert client.get("/boom").status_code == 400

        records = [r for r in caplog.records if r.name == "app.middleware.request_logging"]
        assert len(records) == 1
        assert records[0].levelno == logging.ERROR
        assert "400" in records[0].getMessage()
