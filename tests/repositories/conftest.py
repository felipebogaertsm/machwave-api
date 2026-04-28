"""Shared fixtures for repository tests.

The repository layer talks to ``app.storage.gcs``; the ``fake_gcs`` fixture
in the top-level ``tests/conftest.py`` swaps those helpers for an in-memory
dict so tests can exercise repositories without hitting GCP.
"""

from __future__ import annotations

# Re-export so test modules can ``from tests.repositories.conftest import FakeGCS``
# for type hints, even though the fixture itself lives in the parent conftest.
from tests.conftest import FakeGCS  # noqa: F401
