"""
Backend regression + new-feature tests for SAP BOM Explorer.

Covers:
- GET /api/bom/search: BOM tree resolution, product_uuid presence, data-accuracy
  regression checks for BOM 5989828 (ConsistencyStatus + ECO heuristic fixes).
- POST /api/bom/standard-costs: live SAP OData standard-cost lookup feature.
- GET /api/bom/connection-status: SAP connectivity smoke check.

NOTE: All calls hit a LIVE SAP Business ByDesign tenant over SOAP/OData.
Tests can take several seconds (search) to 10-30s (large BOM cost lookup).
"""
import os

import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
API = f"{BASE_URL}/api"


@pytest.fixture(scope="module")
def session():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


def _collect_uuids(nodes):
    uuids = []
    for n in nodes:
        if n.get("product_uuid"):
            uuids.append(n["product_uuid"])
        uuids.extend(_collect_uuids(n.get("children", [])))
    return uuids


def _find_by_product_id(nodes, product_id):
    found = []
    for n in nodes:
        if n["product_id"] == product_id:
            found.append(n)
        found.extend(_find_by_product_id(n.get("children", []), product_id))
    return found


class TestConnectionStatus:
    def test_connection_status_ok(self, session):
        resp = session.get(f"{API}/bom/connection-status", timeout=30)
        assert resp.status_code == 200
        data = resp.json()
        assert "connected" in data
        assert isinstance(data["connected"], bool)


class TestBomSearchP26584:
    """Verify BOM P26584 resolves correctly with product_uuid present."""

    @pytest.fixture(scope="class")
    def bom_data(self, session):
        resp = session.get(f"{API}/bom/search", params={"bom_id": "P26584"}, timeout=60)
        assert resp.status_code == 200
        return resp.json()

    def test_resolves_to_correct_variant(self, bom_data):
        assert bom_data["bom_id"] == "P26584_3"

    def test_component_count_and_levels(self, bom_data):
        assert bom_data["total_components"] == 7
        assert bom_data["max_level"] == 2

    def test_product_uuid_present_on_all_nodes(self, bom_data):
        uuids = _collect_uuids(bom_data["tree"])
        assert len(uuids) == 7
        for u in uuids:
            assert isinstance(u, str) and len(u) > 0

    def test_nested_children_present(self, bom_data):
        p26688 = _find_by_product_id(bom_data["tree"], "P26688")
        assert len(p26688) == 1
        rod9 = _find_by_product_id(p26688[0]["children"], "ROD-9")
        assert len(rod9) == 1

        p26680 = _find_by_product_id(bom_data["tree"], "P26680")
        assert len(p26680) == 1
        sh6 = _find_by_product_id(p26680[0]["children"], "SH6.0HR")
        assert len(sh6) == 1


class TestStandardCosts:
    """New live SAP OData standard-cost lookup feature."""

    @pytest.fixture(scope="class")
    def bom_data(self, session):
        resp = session.get(f"{API}/bom/search", params={"bom_id": "P26584"}, timeout=60)
        return resp.json()

    def test_costs_endpoint_returns_expected_values(self, session, bom_data):
        uuids = _collect_uuids(bom_data["tree"])
        resp = session.post(f"{API}/bom/standard-costs", json={"product_uuids": uuids}, timeout=60)
        assert resp.status_code == 200
        costs = resp.json()["costs"]

        mps_b = _find_by_product_id(bom_data["tree"], "MPS-B")[0]
        zinc = _find_by_product_id(bom_data["tree"], "ZINC-ING")[0]

        mps_cost = costs.get(mps_b["product_uuid"].upper())
        zinc_cost = costs.get(zinc["product_uuid"].upper())

        assert mps_cost is not None
        assert zinc_cost is not None
        assert mps_cost["currency"] == "INR"
        assert zinc_cost["currency"] == "INR"
        # Live data - values may drift slightly, but should be in a sane range
        assert isinstance(mps_cost["amount"], float) and mps_cost["amount"] > 0
        assert isinstance(zinc_cost["amount"], float) and zinc_cost["amount"] > 0

    def test_costs_endpoint_empty_uuid_list(self, session):
        resp = session.post(f"{API}/bom/standard-costs", json={"product_uuids": []}, timeout=30)
        assert resp.status_code == 200
        assert resp.json()["costs"] == {}


class TestBom5989828Regression:
    """Data-accuracy regression checks fixed this session."""

    @pytest.fixture(scope="class")
    def bom_data(self, session):
        resp = session.get(f"{API}/bom/search", params={"bom_id": "5989828"}, timeout=60)
        assert resp.status_code == 200
        return resp.json()

    def test_component_count(self, bom_data):
        assert bom_data["total_components"] == 109

    def test_5989828_24_quantity_not_stale(self, bom_data):
        matches = _find_by_product_id(bom_data["tree"], "5989828-24")
        assert len(matches) >= 1
        for m in matches:
            assert m["quantity"] == 2.0
            assert m["unit_of_measure"] == "EA"

    def test_hrpo_quantity_not_stale(self, bom_data):
        matches = _find_by_product_id(bom_data["tree"], "HRPO1.8X45.1")
        assert len(matches) >= 1
        for m in matches:
            assert m["quantity"] == pytest.approx(0.1)


class TestBomSearchErrorHandling:
    def test_invalid_bom_returns_404(self, session):
        resp = session.get(f"{API}/bom/search", params={"bom_id": "INVALID_XYZ_999"}, timeout=60)
        assert resp.status_code == 404
        assert "detail" in resp.json()
