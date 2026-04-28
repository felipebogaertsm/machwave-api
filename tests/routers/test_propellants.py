"""Tests for ``GET /propellants``."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.schemas.motor import BILIQUID_FORMULATIONS, SOLID_FORMULATIONS
from tests.conftest import login_as


class TestListPropellants:
    def test_no_auth_required(self, client: TestClient) -> None:
        """The catalogue is read-only and not user-scoped — no auth dependency."""
        assert client.get("/propellants").status_code == 200

    def test_returns_solids_and_liquids(self, client: TestClient) -> None:
        body = client.get("/propellants").json()
        ids = {item["id"] for item in body}
        # Every formulation registered in the schema appears in the response.
        assert ids == set(SOLID_FORMULATIONS) | set(BILIQUID_FORMULATIONS)

    def test_motor_type_tagged_correctly(self, client: TestClient) -> None:
        body = client.get("/propellants").json()
        for item in body:
            if item["id"] in SOLID_FORMULATIONS:
                assert item["motor_type"] == "solid"
            elif item["id"] in BILIQUID_FORMULATIONS:
                assert item["motor_type"] == "liquid"

    def test_solids_listed_before_liquids_each_alphabetised(self, client: TestClient) -> None:
        """The router sorts each section alphabetically and concatenates
        solids → liquids. Lock that order in so the frontend can rely on it."""
        body = client.get("/propellants").json()
        types = [item["motor_type"] for item in body]

        # Section order: every "solid" appears before every "liquid".
        solid_indices = [i for i, t in enumerate(types) if t == "solid"]
        liquid_indices = [i for i, t in enumerate(types) if t == "liquid"]
        if solid_indices and liquid_indices:
            assert max(solid_indices) < min(liquid_indices)

        solid_ids = [item["id"] for item in body if item["motor_type"] == "solid"]
        liquid_ids = [item["id"] for item in body if item["motor_type"] == "liquid"]
        assert solid_ids == sorted(solid_ids)
        assert liquid_ids == sorted(liquid_ids)

    def test_listing_works_authenticated_too(self, app: FastAPI, client: TestClient) -> None:
        """Auth is allowed but ignored — make sure the route still works
        when a token is present so logged-in clients aren't broken."""
        login_as(app, role="member")
        assert client.get("/propellants").status_code == 200
