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
        assert data["total_components"] == 23, f"Expected 23 got {data['total_components']}"
        assert data["max_level"] == 2, f"Expected max_level=2 got {data['max_level']}"
        assert isinstance(data["rows"], list) and len(data["rows"]) == 23
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
        assert data["total_components"] == 91, f"Expected 91 got {data['total_components']}"
        assert data["max_level"] == 5, f"Expected max_level=5 got {data['max_level']}"
        assert len(data["rows"]) == 91
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
        # All rows should be active (inactive/deleted items are now filtered out)
        active_count = sum(1 for r in data["rows"] if r["active"])
        assert active_count == 91, f"Expected all 91 rows active, got {active_count}"

        # ---- Precision bug fix: latest ChangeState (revision) must be selected ----
        # The INSTRUCTION MANUAL level-1 row must show the CURRENT revision:
        #   product_id=6902-602142, description=INSTRUCTION MANUAL FLT2, eco_id=FLT2_4.4
        # NOT the stale prior revision: 6902-602120 / FLT2 INSTRUCTION MANUAL / FLT2_4.3
        manual_rows = [
            r for r in data["rows"]
            if r["level"] == 1 and "INSTRUCTION MANUAL" in (r.get("description") or "").upper()
        ]
        assert manual_rows, "Expected a level-1 INSTRUCTION MANUAL row"
        assert len(manual_rows) == 1, f"Expected exactly one instruction manual level-1 row, got {len(manual_rows)}"
        m = manual_rows[0]
        assert m["product_id"] == "6902-602142", f"Stale product_id: {m['product_id']}"
        assert m["description"].upper().strip() == "INSTRUCTION MANUAL FLT2", f"Unexpected desc: {m['description']}"
        assert m["eco_id"] == "FLT2_4.4", f"Stale eco_id: {m['eco_id']}"

        # Ensure stale values do not appear ANYWHERE in the response
        for row in data["rows"]:
            assert row.get("product_id") != "6902-602120", "Stale product_id 6902-602120 found"
            desc_upper = (row.get("description") or "").upper()
            assert desc_upper != "FLT2 INSTRUCTION MANUAL", f"Stale description found on row {row}"
            # FLT2_4.3 is a stale ECO on this specific line; ensure the manual line isn't showing it
            if row["level"] == 1 and "INSTRUCTION MANUAL" in desc_upper:
                assert row["eco_id"] != "FLT2_4.3", "Stale eco FLT2_4.3 on instruction manual row"


# ---- NEW FEATURE: bare part number auto-resolves to latest revision ----
class TestBarePartNumberResolution:
    """When user enters a bare part number (no _1/_2 suffix), backend must
    fall back to output-product resolution and pick the latest revision."""

    def test_bare_8060522_resolves_to_8060522_1(self, api):
        r = api.get(f"{BASE_URL}/api/bom/search", params={"bom_id": "8060522"}, timeout=120)
        assert r.status_code == 200, r.text[:500]
        data = r.json()
        assert data["bom_id"] == "8060522_1", f"Expected resolved bom_id='8060522_1', got {data['bom_id']}"
        assert data["total_components"] == 23, f"Expected 23 got {data['total_components']}"
        assert data["max_level"] == 2, f"Expected max_level=2 got {data['max_level']}"
        assert len(data["rows"]) == 23

    def test_bare_6800_004061_resolves_to_latest_revision_2(self, api):
        r = api.get(f"{BASE_URL}/api/bom/search", params={"bom_id": "6800-004061"}, timeout=120)
        assert r.status_code == 200, r.text[:500]
        data = r.json()
        assert data["bom_id"] == "6800-004061_2", (
            f"Expected latest revision '6800-004061_2', got {data['bom_id']} "
            "(must NOT resolve to old _1 revision)"
        )
        assert data["total_components"] == 19, f"Expected 19 got {data['total_components']}"

    def test_totally_invalid_part_returns_404(self, api):
        r = api.get(f"{BASE_URL}/api/bom/search", params={"bom_id": "NOTAREALPART999"}, timeout=60)
        assert r.status_code == 404, r.text[:500]
        assert "not found" in r.json().get("detail", "").lower()


# ---- Stability / reliability regression: retry fix for transient SAP timeouts ----
# NOTE: kept in TestBomSearch class so pytest-xdist loadscope pins it to the same worker
# as test_search_flt2_4_1_multilevel; two concurrent FLT2 explosions on different workers
# can overload SAP / the preview ingress (502).
class TestFlt2StabilityAcrossRepeatedRuns:
    """Run FLT2_4.1 four times in a row; must return 91/5 every time with correct manual row."""

    def test_flt2_4_1_stable_across_4_runs(self, api):
        results = []
        for i in range(4):
            r = api.get(f"{BASE_URL}/api/bom/search", params={"bom_id": "FLT2_4.1"}, timeout=180)
            assert r.status_code == 200, f"Run {i+1}: HTTP {r.status_code} - {r.text[:300]}"
            data = r.json()
            manual = [
                x for x in data["rows"]
                if x["level"] == 1 and "INSTRUCTION MANUAL" in (x.get("description") or "").upper()
            ]
            results.append({
                "run": i + 1,
                "total": data["total_components"],
                "max_level": data["max_level"],
                "rows_len": len(data["rows"]),
                "manual_pid": manual[0]["product_id"] if manual else None,
                "manual_eco": manual[0]["eco_id"] if manual else None,
            })
        print(f"\nStability results: {results}")
        for res in results:
            assert res["total"] == 91, f"Run {res['run']}: total_components={res['total']} (expected 91)"
            assert res["max_level"] == 5, f"Run {res['run']}: max_level={res['max_level']} (expected 5)"
            assert res["rows_len"] == 91, f"Run {res['run']}: rows_len={res['rows_len']} (expected 91)"
            assert res["manual_pid"] == "6902-602142", f"Run {res['run']}: manual pid={res['manual_pid']}"
            assert res["manual_eco"] == "FLT2_4.4", f"Run {res['run']}: manual eco={res['manual_eco']}"
