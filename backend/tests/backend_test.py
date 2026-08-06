"""Backend tests for SAP BOM Lookup API (SOAP multi-level explosion, tree response)."""
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


# ---- Helpers to flatten tree responses ----
def flatten_tree(nodes):
    """Recursively flatten a tree of BomNode-dicts to a flat list."""
    out = []
    for n in nodes:
        out.append(n)
        children = n.get("children") or []
        out.extend(flatten_tree(children))
    return out


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


# ---- BOM search (multi-level tree) ----
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

    # ---- Response shape: tree, not rows ----
    def test_response_shape_has_tree_field(self, api):
        r = api.get(f"{BASE_URL}/api/bom/search", params={"bom_id": "8060522_1"}, timeout=120)
        assert r.status_code == 200, r.text[:500]
        data = r.json()
        # New tree field
        assert "tree" in data, f"Response missing 'tree' field: keys={list(data.keys())}"
        assert isinstance(data["tree"], list)
        assert "total_components" in data
        assert "max_level" in data

    # ---- Regression: 8060522_1 multi-level (23/2) - tree structure ----
    def test_search_8060522_1_regression_tree(self, api):
        r = api.get(f"{BASE_URL}/api/bom/search", params={"bom_id": "8060522_1"}, timeout=120)
        assert r.status_code == 200, r.text[:500]
        data = r.json()
        assert data["bom_id"] == "8060522_1"
        assert data["total_components"] == 23, f"Expected 23 got {data['total_components']}"
        assert data["max_level"] == 2, f"Expected max_level=2 got {data['max_level']}"

        tree = data["tree"]
        # Top-level should be 10 nodes (actual SAP data; problem statement said 8 but SAP returns 10)
        assert len(tree) == 10, f"Expected 10 Level-1 nodes at tree root; got {len(tree)}"
        # Flatten and validate full component count
        flat = flatten_tree(tree)
        assert len(flat) == 23, f"Flattened tree has {len(flat)} nodes, expected 23"
        # All top-level nodes should have level==1
        for node in tree:
            assert node["level"] == 1, f"Top-level node has level={node['level']}"
            assert "children" in node
        # Validate node schema
        node = tree[0]
        for k in ("level", "product_id", "description", "quantity", "unit_of_measure", "eco_id", "active", "has_sub_bom", "children"):
            assert k in node, f"Node missing key {k}"

    # ---- FLT2_4.1 multi-level explosion (91/5) - tree structure ----
    def test_search_flt2_4_1_multilevel_tree(self, api):
        r = api.get(f"{BASE_URL}/api/bom/search", params={"bom_id": "FLT2_4.1"}, timeout=180)
        assert r.status_code == 200, r.text[:500]
        data = r.json()
        assert data["bom_id"] == "FLT2_4.1"
        assert data["total_components"] == 91, f"Expected 91 got {data['total_components']}"
        assert data["max_level"] == 5, f"Expected max_level=5 got {data['max_level']}"

        tree = data["tree"]
        # Should be 16 Level-1 nodes per problem statement
        assert len(tree) == 16, f"Expected 16 Level-1 nodes; got {len(tree)}"
        flat = flatten_tree(tree)
        assert len(flat) == 91, f"Flattened tree has {len(flat)} nodes, expected 91"

        # Level distribution
        levels = {n["level"] for n in flat}
        assert levels.issubset({1, 2, 3, 4, 5})
        assert 1 in levels and 5 in levels

        # Spot-check known Level-1 6700-303008
        target = [n for n in tree if n["product_id"] == "6700-303008"]
        assert target, "Expected level-1 node for 6700-303008"
        assert target[0]["quantity"] == 0.5
        assert target[0]["unit_of_measure"] == "EA"

        # ---- Latest revision must be selected for instruction manual ----
        manual_rows = [
            n for n in tree
            if n["level"] == 1 and "INSTRUCTION MANUAL" in (n.get("description") or "").upper()
        ]
        assert manual_rows, "Expected a level-1 INSTRUCTION MANUAL row"
        assert len(manual_rows) == 1
        m = manual_rows[0]
        assert m["product_id"] == "6902-602142", f"Stale product_id: {m['product_id']}"
        assert m["description"].upper().strip() == "INSTRUCTION MANUAL FLT2"
        assert m["eco_id"] == "FLT2_4.4"

        # Ensure stale values do not appear anywhere in flat tree
        for node in flat:
            assert node.get("product_id") != "6902-602120", "Stale product_id 6902-602120 found"
            desc_upper = (node.get("description") or "").upper()
            assert desc_upper != "FLT2 INSTRUCTION MANUAL", f"Stale description found on node {node}"


# ---- Bare part number resolution ----
class TestBarePartNumberResolution:
    def test_bare_8060522_resolves_to_8060522_1(self, api):
        r = api.get(f"{BASE_URL}/api/bom/search", params={"bom_id": "8060522"}, timeout=120)
        assert r.status_code == 200, r.text[:500]
        data = r.json()
        assert data["bom_id"] == "8060522_1"
        assert data["total_components"] == 23
        assert data["max_level"] == 2
        flat = flatten_tree(data["tree"])
        assert len(flat) == 23

    def test_bare_6800_004061_resolves_to_latest_revision_2(self, api):
        r = api.get(f"{BASE_URL}/api/bom/search", params={"bom_id": "6800-004061"}, timeout=120)
        assert r.status_code == 200, r.text[:500]
        data = r.json()
        assert data["bom_id"] == "6800-004061_2"
        assert data["total_components"] == 19

    def test_totally_invalid_part_returns_404(self, api):
        r = api.get(f"{BASE_URL}/api/bom/search", params={"bom_id": "NOTAREALPART999"}, timeout=60)
        assert r.status_code == 404, r.text[:500]
        assert "not found" in r.json().get("detail", "").lower()
