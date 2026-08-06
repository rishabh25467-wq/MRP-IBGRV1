"""Backend tests for SAP BOM Lookup API (SOAP multi-level explosion)."""
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


# ---- Connection status (SOAP endpoint) ----
class TestConnectionStatus:
    def test_connection_status_connected(self, api):
        r = api.get(f"{BASE_URL}/api/bom/connection-status", timeout=60)
        assert r.status_code == 200
        data = r.json()
        assert data.get("connected") is True, f"Expected connected=True; got {data}"
        assert isinstance(data.get("message"), str)


# ---- BOM search (multi-level flat rows) ----
class TestBomSearch:
    def test_search_invalid_bom_returns_404(self, api):
        r = api.get(f"{BASE_URL}/api/bom/search", params={"bom_id": "INVALID_BOM_999"}, timeout=60)
        assert r.status_code == 404, r.text[:500]
        assert "not found" in r.json().get("detail", "").lower()

    def test_search_missing_bom_id_returns_422(self, api):
        r = api.get(f"{BASE_URL}/api/bom/search", timeout=30)
        assert r.status_code == 422

    def test_search_empty_bom_id_returns_422(self, api):
        r = api.get(f"{BASE_URL}/api/bom/search", params={"bom_id": ""}, timeout=30)
        assert r.status_code == 422

    def test_search_bom_whitespace_trimmed(self, api):
        r = api.get(f"{BASE_URL}/api/bom/search", params={"bom_id": "  8060522_1  "}, timeout=120)
        assert r.status_code == 200
        assert r.json()["bom_id"] == "8060522_1"

    # ---- Regression: 8060522_1 now multi-level (33/2) ----
    def test_search_8060522_1_regression(self, api):
        r = api.get(f"{BASE_URL}/api/bom/search", params={"bom_id": "8060522_1"}, timeout=120)
        assert r.status_code == 200, r.text[:500]
        data = r.json()
        assert data["bom_id"] == "8060522_1"
        assert data["total_components"] == 33, f"Expected 33 got {data['total_components']}"
        assert data["max_level"] == 2, f"Expected max_level=2 got {data['max_level']}"
        assert isinstance(data["rows"], list) and len(data["rows"]) == 33
        # Validate row schema
        row = data["rows"][0]
        for k in ("level", "product_id", "description", "quantity", "unit_of_measure", "eco_id", "active", "has_sub_bom"):
            assert k in row, f"Row missing key {k}"
        assert row["level"] == 1

    # ---- Main feature: FLT2_4.1 multi-level explosion (145/5) ----
    def test_search_flt2_4_1_multilevel(self, api):
        r = api.get(f"{BASE_URL}/api/bom/search", params={"bom_id": "FLT2_4.1"}, timeout=120)
        assert r.status_code == 200, r.text[:500]
        data = r.json()
        assert data["bom_id"] == "FLT2_4.1"
        assert data["total_components"] == 145, f"Expected 145 got {data['total_components']}"
        assert data["max_level"] == 5, f"Expected max_level=5 got {data['max_level']}"
        assert len(data["rows"]) == 145
        # Level distribution
        levels = {row["level"] for row in data["rows"]}
        assert levels.issubset({1, 2, 3, 4, 5})
        assert 1 in levels and 5 in levels
        # Spot-check known row: Level 1 6700-303008 qty=0.5 UOM=EA
        target = [r for r in data["rows"] if r["level"] == 1 and r["product_id"] == "6700-303008"]
        assert target, "Expected level-1 row for 6700-303008"
        assert target[0]["quantity"] == 0.5
        assert target[0]["unit_of_measure"] == "EA"
        # Verify has_sub_bom flag exists on some rows
        assert any(r["has_sub_bom"] for r in data["rows"]), "Expected at least one has_sub_bom=True row"
        # Active count close to 144
        active_count = sum(1 for r in data["rows"] if r["active"])
        assert active_count >= 140, f"Expected ~144 active, got {active_count}"
