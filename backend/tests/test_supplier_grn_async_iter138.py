"""Supplier Portal GRN async (Playwright) flow - iteration 138.

Covers the READ + VALIDATION surface of the new async approve pipeline:
  GET  /api/admin/grn/sites
  GET  /api/admin/grn/warehouses/{site_id}
  GET  /api/admin/grn/shipments
  GET  /api/admin/grn/lookup/{doc_code}
  GET  /api/admin/grn/receipt-status/{job_id}
  POST /api/admin/grn/{doc_code}/approve            (validation only here)
  POST /api/admin/grn/{doc_code}/retry-goods-receipt (guard only here)
  POST /api/admin/grn/{doc_code}/retry-movement      (guard only here)

The real approve (which triggers a live Playwright SAP login against the
deliberately FAKE PO 99999001) is exercised once from the frontend
Playwright test; this file only asserts its guards so the single test
shipment TESTG1 is not consumed here.
"""
import os

import pytest
import requests
from dotenv import dotenv_values

frontend_env = dotenv_values("/app/frontend/.env")
base_url = os.environ.get("REACT_APP_BACKEND_URL") or frontend_env.get("REACT_APP_BACKEND_URL")
if not base_url:
    raise RuntimeError("REACT_APP_BACKEND_URL missing")
BASE_URL = base_url.rstrip("/")
SESSION_COOKIE = "KGABHXazt4DGxMLppFGLi9bbLu25Z9m9"
TEST_CODE = "TESTG1"
FAKE_PO = "99999001"


@pytest.fixture(scope="module")
def client():
    s = requests.Session()
    s.cookies.set("vms_session", SESSION_COOKIE)
    s.headers.update({"Content-Type": "application/json"})
    return s


class TestGrnReadEndpoints:
    def test_sites(self, client):
        r = client.get(f"{BASE_URL}/api/admin/grn/sites", timeout=60)
        assert r.status_code == 200, r.text[:300]
        sites = r.json()["sites"]
        assert isinstance(sites, list) and len(sites) > 0
        assert "P7" in sites

    def test_warehouses_for_site(self, client):
        r = client.get(f"{BASE_URL}/api/admin/grn/warehouses/P7", timeout=120)
        assert r.status_code == 200, r.text[:300]
        whs = r.json()["warehouses"]
        assert isinstance(whs, list) and len(whs) > 0
        assert "warehouse_id" in whs[0]

    def test_shipments_filter_and_no_mongo_id_leak(self, client):
        r = client.get(f"{BASE_URL}/api/admin/grn/shipments", params={"status": "in_transit"}, timeout=60)
        assert r.status_code == 200
        shipments = r.json()["shipments"]
        assert any(s["_id"] == TEST_CODE for s in shipments)
        for s in shipments:
            assert isinstance(s["_id"], str)  # doc code, not ObjectId

    def test_lookup_test_shipment_structure(self, client):
        r = client.get(f"{BASE_URL}/api/admin/grn/lookup/{TEST_CODE}", timeout=60)
        assert r.status_code == 200, r.text[:300]
        doc = r.json()
        assert doc["_id"] == TEST_CODE
        assert doc["vendor_code"] == "H1330"
        assert len(doc["items"]) == 2
        assert {it["item_number"] for it in doc["items"]} == {"10", "20"}
        assert all(it["po_number"] == FAKE_PO for it in doc["items"])
        qtys = {it["item_number"]: it["ship_qty"] for it in doc["items"]}
        assert qtys == {"10": 100, "20": 50}

    def test_lookup_unknown_code_404(self, client):
        r = client.get(f"{BASE_URL}/api/admin/grn/lookup/ZZZZZZ", timeout=60)
        assert r.status_code == 404
        assert "detail" in r.json()

    def test_receipt_status_unknown_job_404(self, client):
        r = client.get(f"{BASE_URL}/api/admin/grn/receipt-status/not-a-real-job", timeout=60)
        assert r.status_code == 404
        assert r.json()["detail"] == "Unknown job_id"


class TestGrnApproveValidation:
    def test_approve_missing_site_id_422(self, client):
        r = client.post(f"{BASE_URL}/api/admin/grn/{TEST_CODE}/approve",
                        json={"supplier_doc_num": "TEST-DOC-1", "bill_date": "2026-07-01"}, timeout=60)
        assert r.status_code == 422, r.text[:300]

    def test_approve_unknown_shipment_404(self, client):
        r = client.post(f"{BASE_URL}/api/admin/grn/ZZZZZZ/approve",
                        json={"supplier_doc_num": "X", "bill_date": "2026-07-01", "site_id": "P7",
                              "warehouse_id": "P7-RM", "item_actual_qtys": []}, timeout=60)
        assert r.status_code == 404, r.text[:300]

    def test_approve_bad_actual_qty_type_422(self, client):
        r = client.post(f"{BASE_URL}/api/admin/grn/{TEST_CODE}/approve",
                        json={"supplier_doc_num": "X", "bill_date": "2026-07-01", "site_id": "P7",
                              "warehouse_id": "P7-RM",
                              "item_actual_qtys": [{"po_number": FAKE_PO, "item_number": "10", "actual_qty": "abc"}]},
                        timeout=60)
        assert r.status_code == 422, r.text[:300]


class TestGrnPostApproveState:
    """State assertions after the (already executed) live approve of TESTG1
    against the fake PO 99999001 - safe, read-only / guard-only."""

    def test_shipment_persisted_approval_fields(self, client):
        r = client.get(f"{BASE_URL}/api/admin/grn/lookup/{TEST_CODE}", timeout=60)
        assert r.status_code == 200
        doc = r.json()
        assert doc["status"] == "approved"
        assert doc["supplier_doc_num"] == "TEST-DOC-1"
        assert doc["bill_date"] == "2026-07-31"
        assert doc["site_id"] == "P7"
        assert doc["warehouse_id"] == "P7-RM"
        qtys = {it["item_number"]: it["actual_qty"] for it in doc["items"]}
        assert qtys == {"10": 95.0, "20": 50.0}, f"staff actual_qty override not persisted: {qtys}"

    def test_fake_po_never_reports_posted(self, client):
        doc = client.get(f"{BASE_URL}/api/admin/grn/lookup/{TEST_CODE}", timeout=60).json()
        assert doc["sap_sync_status"] == "pending", f"fake PO must never post, got {doc['sap_sync_status']}"
        per_po = doc["sap_gr_result"]["per_po"]
        assert per_po[0]["po_number"] == FAKE_PO
        assert per_po[0]["status"] == "skipped"
        assert "not found" in per_po[0]["error"].lower()
        assert doc["sap_movement_status"] == "not_applicable"

    def test_double_approve_rejected(self, client):
        r = client.post(f"{BASE_URL}/api/admin/grn/{TEST_CODE}/approve",
                        json={"supplier_doc_num": "X", "bill_date": "2026-07-31", "site_id": "P7",
                              "warehouse_id": "P7-RM", "item_actual_qtys": []}, timeout=60)
        assert r.status_code == 400
        assert "already approved" in r.json()["detail"]

    def test_retry_movement_blocked_while_gr_not_posted(self, client):
        r = client.post(f"{BASE_URL}/api/admin/grn/{TEST_CODE}/retry-movement", timeout=60)
        assert r.status_code == 400
        assert "step 1" in r.json()["detail"]

    def test_approve_requires_auth(self):
        r = requests.post(f"{BASE_URL}/api/admin/grn/{TEST_CODE}/approve",
                          json={"site_id": "P7", "warehouse_id": "P7-RM"}, timeout=60)
        assert r.status_code == 401


class TestGrnRetryGuards:
    def test_retry_goods_receipt_unknown_shipment(self, client):
        r = client.post(f"{BASE_URL}/api/admin/grn/ZZZZZZ/retry-goods-receipt", timeout=60)
        assert r.status_code in (404, 500), r.text[:300]
        assert r.status_code == 404, f"expected 404 for unknown shipment, got {r.status_code}: {r.text[:300]}"

    def test_retry_movement_unknown_shipment(self, client):
        r = client.post(f"{BASE_URL}/api/admin/grn/ZZZZZZ/retry-movement", timeout=60)
        assert r.status_code == 404, r.text[:300]
