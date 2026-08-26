"""Iteration 125 - GRN Phase 4: Site/Warehouse selection (bound-site lock),
Supplier Doc Num, discrepancy mark/auto-clear flow, 2-step SAP write tracking.

Covers:
  GET  /api/admin/grn/sites
  GET  /api/admin/grn/warehouses/{site_id}
  POST /api/admin/grn/{doc_code}/discrepancy
  POST /api/admin/grn/{doc_code}/approve   (body: supplier_doc_num, site_id, warehouse_id)
  POST /api/admin/grn/{doc_code}/reject
  POST /api/admin/grn/{doc_code}/retry-movement
  PUT  /api/supplier-portal/shipments/{doc_code}  (vendor edit auto-clears discrepancy)
"""
import os

import pytest
import requests
from dotenv import dotenv_values

_env = dotenv_values("/app/frontend/.env")
BASE_URL = (os.environ.get("REACT_APP_BACKEND_URL") or _env.get("REACT_APP_BACKEND_URL")).rstrip("/")
API = f"{BASE_URL}/api"

GRN_SESSION = "XAlQkzY5hG8PB3MdokeHOlLvQVFkAQuFFqwSgDD6Dpk"
VENDOR_EMAIL = "vendor1@testco.com"
VENDOR_PASSWORD = "DummyTest123"
BOUND_SITE = "P2"


@pytest.fixture(scope="module")
def staff():
    s = requests.Session()
    s.cookies.set("vms_session", GRN_SESSION)
    return s


@pytest.fixture(scope="module")
def vendor():
    s = requests.Session()
    r = s.post(f"{API}/supplier-portal/login", json={"email": VENDOR_EMAIL, "password": VENDOR_PASSWORD}, timeout=60)
    if r.status_code != 200:
        pytest.fail(f"Vendor login failed {r.status_code}: {r.text[:300]}")
    return s


def _new_shipment(vendor_session, items):
    r = vendor_session.post(f"{API}/supplier-portal/shipments", json={"items": items}, timeout=90)
    assert r.status_code == 200, f"create shipment failed {r.status_code}: {r.text[:400]}"
    doc = r.json()
    assert doc["status"] == "in_transit"
    assert "_id" in doc and len(doc["_id"]) == 6
    return doc


# ---- Site / warehouse scoping ----
class TestSitesAndWarehouses:
    def test_sites_only_bound_site(self, staff):
        import time
        t0 = time.time()
        r = staff.get(f"{API}/admin/grn/sites", timeout=120)
        elapsed = time.time() - t0
        assert r.status_code == 200, r.text[:300]
        print(f"[perf] /admin/grn/sites took {elapsed:.1f}s")
        sites = r.json()["sites"]
        ids = [s if isinstance(s, str) else s.get("site_id") or s.get("id") for s in sites]
        assert ids == [BOUND_SITE], f"expected only {BOUND_SITE}, got {sites}"

    def test_warehouses_for_bound_site(self, staff):
        r = staff.get(f"{API}/admin/grn/warehouses/{BOUND_SITE}", timeout=90)
        assert r.status_code == 200, r.text[:300]
        whs = r.json()["warehouses"]
        assert isinstance(whs, list) and len(whs) > 0, "no warehouses returned for P2"
        flat = str(whs)
        assert "P2-" in flat, f"warehouse ids do not look site-scoped: {whs[:3]}"

    def test_warehouses_forbidden_for_unbound_site(self, staff):
        r = staff.get(f"{API}/admin/grn/warehouses/P1", timeout=60)
        assert r.status_code == 403, f"expected 403 for unbound site, got {r.status_code}: {r.text[:200]}"

    def test_sites_requires_auth(self):
        r = requests.get(f"{API}/admin/grn/sites", timeout=60)
        assert r.status_code in (401, 403), f"unauthenticated access returned {r.status_code}"


# ---- Discrepancy flow ----
class TestDiscrepancyFlow:
    def test_discrepancy_validation_and_mark(self, staff, vendor):
        ship = _new_shipment(vendor, [
            {"po_number": "TESTGRN1", "item_number": "1", "ship_qty": 2},
            {"po_number": "TESTGRN1", "item_number": "2", "ship_qty": 1},
        ])
        code = ship["_id"]

        # reason present, no items -> 400
        r = staff.post(f"{API}/admin/grn/{code}/discrepancy", json={"reason": "TEST_short qty", "items": []}, timeout=60)
        assert r.status_code == 400, f"empty items accepted: {r.status_code} {r.text[:200]}"

        # items present, empty reason -> 400
        r = staff.post(f"{API}/admin/grn/{code}/discrepancy",
                       json={"reason": "   ", "items": [{"po_number": "TESTGRN1", "item_number": "1"}]}, timeout=60)
        assert r.status_code == 400, f"empty reason accepted: {r.status_code}"

        # item not part of shipment -> 400
        r = staff.post(f"{API}/admin/grn/{code}/discrepancy",
                       json={"reason": "TEST_x", "items": [{"po_number": "TESTGRN2", "item_number": "1"}]}, timeout=60)
        assert r.status_code == 400, f"foreign item accepted: {r.status_code}"

        # valid
        r = staff.post(f"{API}/admin/grn/{code}/discrepancy",
                       json={"reason": "TEST_only 1 of 2 boxes received",
                             "items": [{"po_number": "TESTGRN1", "item_number": "2"}]}, timeout=60)
        assert r.status_code == 200, r.text[:400]
        doc = r.json()
        assert doc["status"] == "discrepancy"
        assert doc["discrepancy_reason"] == "TEST_only 1 of 2 boxes received"
        assert doc["discrepancy_items"] == [{"po_number": "TESTGRN1", "item_number": "2"}]
        assert doc["discrepancy_marked_by"]
        assert doc["discrepancy_marked_at"]
        assert doc.get("sap_sync_status") in (None, "pending", "not_applicable")

        # persisted
        g = staff.get(f"{API}/admin/grn/lookup/{code}", timeout=60)
        assert g.status_code == 200
        assert g.json()["status"] == "discrepancy"

        # cannot re-mark an already-discrepancy shipment
        r = staff.post(f"{API}/admin/grn/{code}/discrepancy",
                       json={"reason": "TEST_again", "items": [{"po_number": "TESTGRN1", "item_number": "1"}]}, timeout=60)
        assert r.status_code == 400, f"re-mark allowed: {r.status_code}"

        # vendor still sees it and can EDIT it -> auto-clears back to in_transit
        v = vendor.get(f"{API}/supplier-portal/shipments", timeout=60)
        assert v.status_code == 200
        row = next(s for s in v.json()["shipments"] if s["_id"] == code)
        assert row["status"] == "discrepancy"
        assert row["discrepancy_reason"] == "TEST_only 1 of 2 boxes received"

        e = vendor.put(f"{API}/supplier-portal/shipments/{code}",
                       json={"items": [{"po_number": "TESTGRN1", "item_number": "1", "ship_qty": 3},
                                       {"po_number": "TESTGRN1", "item_number": "2", "ship_qty": 2}]}, timeout=90)
        assert e.status_code == 200, f"vendor edit of discrepancy shipment failed: {e.status_code} {e.text[:300]}"
        edited = e.json()
        assert edited["status"] == "in_transit", f"discrepancy not auto-cleared: {edited['status']}"
        assert edited["discrepancy_reason"] is None
        assert edited["discrepancy_items"] is None
        assert edited["discrepancy_marked_by"] is None

        g = staff.get(f"{API}/admin/grn/lookup/{code}", timeout=60)
        assert g.json()["status"] == "in_transit"
        assert {i["item_number"]: i["ship_qty"] for i in g.json()["items"]} == {"1": 3.0, "2": 2.0}

        # reject from in_transit works
        r = staff.post(f"{API}/admin/grn/{code}/reject", json={"reason": "TEST_cleanup reject"}, timeout=60)
        assert r.status_code == 200, r.text[:300]
        assert r.json()["status"] == "rejected"

    def test_reject_from_discrepancy(self, staff, vendor):
        ship = _new_shipment(vendor, [{"po_number": "TESTGRN2", "item_number": "1", "ship_qty": 1}])
        code = ship["_id"]
        r = staff.post(f"{API}/admin/grn/{code}/discrepancy",
                       json={"reason": "TEST_damaged carton", "items": [{"po_number": "TESTGRN2", "item_number": "1"}]}, timeout=60)
        assert r.status_code == 200 and r.json()["status"] == "discrepancy"
        r = staff.post(f"{API}/admin/grn/{code}/reject", json={"reason": "TEST_reject from discrepancy"}, timeout=60)
        assert r.status_code == 200, f"reject from discrepancy failed: {r.status_code} {r.text[:300]}"
        d = r.json()
        assert d["status"] == "rejected"
        assert d["rejection_reason"] == "TEST_reject from discrepancy"


# ---- Approve: 2-step SAP write ----
class TestApproveTwoStep:
    def test_approve_site_access_enforced(self, staff, vendor):
        ship = _new_shipment(vendor, [{"po_number": "TESTGRN2", "item_number": "1", "ship_qty": 1}])
        code = ship["_id"]
        r = staff.post(f"{API}/admin/grn/{code}/approve",
                       json={"supplier_doc_num": "TEST_INV-1", "site_id": "P1", "warehouse_id": "P1-RM"}, timeout=120)
        assert r.status_code == 403, f"approve allowed on unbound site: {r.status_code}"
        assert staff.get(f"{API}/admin/grn/lookup/{code}", timeout=60).json()["status"] == "in_transit"
        # cleanup
        staff.post(f"{API}/admin/grn/{code}/reject", json={"reason": "TEST_cleanup"}, timeout=60)

    def test_approve_missing_site_id_rejected(self, staff, vendor):
        ship = _new_shipment(vendor, [{"po_number": "TESTGRN2", "item_number": "1", "ship_qty": 1}])
        code = ship["_id"]
        r = staff.post(f"{API}/admin/grn/{code}/approve", json={"supplier_doc_num": "TEST_INV-2"}, timeout=60)
        assert r.status_code == 422, f"expected validation error, got {r.status_code}"
        staff.post(f"{API}/admin/grn/{code}/reject", json={"reason": "TEST_cleanup"}, timeout=60)

    def test_approve_from_discrepancy_two_step(self, staff, vendor):
        ship = _new_shipment(vendor, [{"po_number": "TESTGRN1", "item_number": "1", "ship_qty": 1}])
        code = ship["_id"]
        staff.post(f"{API}/admin/grn/{code}/discrepancy",
                   json={"reason": "TEST_override path", "items": [{"po_number": "TESTGRN1", "item_number": "1"}]}, timeout=60)

        # retry-movement before approval -> 400
        r = staff.post(f"{API}/admin/grn/{code}/retry-movement", json={}, timeout=90)
        assert r.status_code == 400, f"retry-movement allowed pre-approval: {r.status_code}"

        r = staff.post(f"{API}/admin/grn/{code}/approve",
                       json={"supplier_doc_num": "TEST_INV-OVERRIDE", "site_id": BOUND_SITE, "warehouse_id": "P2-RM"}, timeout=240)
        assert r.status_code == 200, f"approve failed (local crash?) {r.status_code}: {r.text[:500]}"
        doc = r.json()
        assert doc["status"] == "approved"
        assert doc["supplier_doc_num"] == "TEST_INV-OVERRIDE"
        assert doc["site_id"] == BOUND_SITE and doc["warehouse_id"] == "P2-RM"
        assert doc["sap_sync_status"] in ("posted", "pending")
        assert doc["sap_gr_result"] is not None
        assert "sap_movement_status" in doc and "sap_movement_result" in doc
        if doc["sap_sync_status"] != "posted":
            # step 2 must be correctly skipped
            assert doc["sap_movement_status"] == "not_applicable", doc["sap_movement_status"]
            assert doc["sap_gr_result"]["ok"] is False
            assert doc["sap_gr_result"].get("reason"), "no SAP-side rejection reason captured"
            # retry-movement must refuse when step 1 never posted
            r2 = staff.post(f"{API}/admin/grn/{code}/retry-movement", json={}, timeout=90)
            assert r2.status_code == 400
        else:
            assert doc["sap_movement_status"] in ("posted", "pending")

        # double approve blocked
        r = staff.post(f"{API}/admin/grn/{code}/approve",
                       json={"supplier_doc_num": "x", "site_id": BOUND_SITE, "warehouse_id": "P2-RM"}, timeout=120)
        assert r.status_code == 400
        # vendor can no longer edit an approved shipment
        e = vendor.put(f"{API}/supplier-portal/shipments/{code}",
                       json={"items": [{"po_number": "TESTGRN1", "item_number": "1", "ship_qty": 2}]}, timeout=90)
        assert e.status_code == 400, f"vendor edited an approved shipment: {e.status_code}"
        # discrepancy on approved blocked
        r = staff.post(f"{API}/admin/grn/{code}/discrepancy",
                       json={"reason": "TEST_late", "items": [{"po_number": "TESTGRN1", "item_number": "1"}]}, timeout=60)
        assert r.status_code == 400


# ---- Regression: pending shipments list ----
class TestRegression:
    def test_pending_shipments_list(self, staff):
        r = staff.get(f"{API}/admin/grn/shipments", params={"status": "in_transit"}, timeout=60)
        assert r.status_code == 200, r.text[:300]
        rows = r.json()["shipments"]
        assert isinstance(rows, list)
        for row in rows:
            assert row["status"] == "in_transit"
            assert "_id" in row and "vendor_code" in row

    def test_discrepancy_status_filter(self, staff):
        r = staff.get(f"{API}/admin/grn/shipments", params={"status": "discrepancy"}, timeout=60)
        assert r.status_code == 200
        for row in r.json()["shipments"]:
            assert row["status"] == "discrepancy"

    def test_lookup_unknown_code_404(self, staff):
        r = staff.get(f"{API}/admin/grn/lookup/ZZZZZZ", timeout=60)
        assert r.status_code == 404
