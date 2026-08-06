"""Backend tests for SAP BOM Lookup API."""
import os
from pathlib import Path

import pytest
import requests
from dotenv import dotenv_values

frontend_env = dotenv_values("/app/frontend/.env")
base_url = os.environ.get("REACT_APP_BACKEND_URL") or frontend_env.get("REACT_APP_BACKEND_URL")
if not base_url:
    raise RuntimeError("REACT_APP_BACKEND_URL missing")
BASE_URL = base_url.rstrip("/")


@pytest.fixture(scope="module")
def api():
    s = requests.Session()
    s.headers.update({"Accept": "application/json"})
    return s


# ---- Root / health ----
class TestRoot:
    def test_root(self, api):
        r = api.get(f"{BASE_URL}/api/", timeout=30)
        assert r.status_code == 200
        assert "message" in r.json()


# ---- Connection status ----
class TestConnectionStatus:
    def test_connection_status_connected(self, api):
        r = api.get(f"{BASE_URL}/api/bom/connection-status", timeout=60)
        assert r.status_code == 200
        data = r.json()
        assert "connected" in data and "message" in data
        assert data["connected"] is True, f"Expected connected=True; got {data}"


# ---- BOM search ----
class TestBomSearch:
    def test_search_valid_bom(self, api):
        r = api.get(f"{BASE_URL}/api/bom/search", params={"bom_id": "8060522_1"}, timeout=90)
        assert r.status_code == 200, r.text[:500]
        data = r.json()
        assert data["bom_id"] == "8060522_1"
        assert isinstance(data["total_components"], int)
        assert data["total_components"] > 0
        assert isinstance(data["total_groups"], int)
        assert data["total_groups"] > 0
        assert isinstance(data["groups"], list)
        # Validate structure of first group/component
        g = data["groups"][0]
        assert "group_id" in g and "components" in g
        assert len(g["components"]) > 0
        c = g["components"][0]
        for k in ("material_id", "quantity", "unit_of_measure", "eco_id", "active"):
            assert k in c

    def test_search_invalid_bom_returns_404(self, api):
        r = api.get(f"{BASE_URL}/api/bom/search", params={"bom_id": "INVALID_BOM_999"}, timeout=60)
        assert r.status_code == 404, r.text[:500]
        detail = r.json().get("detail", "")
        assert "not found" in detail.lower()

    def test_search_missing_bom_id_returns_422(self, api):
        r = api.get(f"{BASE_URL}/api/bom/search", timeout=30)
        assert r.status_code == 422

    def test_search_empty_bom_id_returns_422(self, api):
        r = api.get(f"{BASE_URL}/api/bom/search", params={"bom_id": ""}, timeout=30)
        assert r.status_code == 422

    def test_search_bom_whitespace_trimmed(self, api):
        r = api.get(f"{BASE_URL}/api/bom/search", params={"bom_id": "  8060522_1  "}, timeout=90)
        assert r.status_code == 200
        assert r.json()["bom_id"] == "8060522_1"

    # ---- Bug fix verification: FLT2_4.1 must return 16 components across 2 groups ----
    def test_search_flt2_4_1_returns_full_components(self, api):
        r = api.get(f"{BASE_URL}/api/bom/search", params={"bom_id": "FLT2_4.1"}, timeout=120)
        assert r.status_code == 200, r.text[:500]
        data = r.json()
        assert data["bom_id"] == "FLT2_4.1"
        assert data["total_groups"] == 2, f"Expected 2 groups, got {data['total_groups']}"
        assert data["total_components"] == 16, f"Expected 16 components, got {data['total_components']}"
        # Sum of components across groups == 16
        total_in_groups = sum(len(g["components"]) for g in data["groups"])
        assert total_in_groups == 16
        # Every component must have required fields populated
        for g in data["groups"]:
            for c in g["components"]:
                assert c.get("material_id"), f"material_id missing: {c}"
                assert c.get("quantity") is not None
                assert c.get("unit_of_measure")
                assert "eco_id" in c
                assert "active" in c

    # ---- Regression: 8060522_1 must still return 10 components across 2 groups ----
    def test_search_8060522_1_regression(self, api):
        r = api.get(f"{BASE_URL}/api/bom/search", params={"bom_id": "8060522_1"}, timeout=90)
        assert r.status_code == 200
        data = r.json()
        assert data["total_groups"] == 2
        assert data["total_components"] == 10
