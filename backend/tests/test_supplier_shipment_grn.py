"""Supplier Portal Phase 3 (shipment + 2-way match) & Phase 4 (internal GRN
approval with best-effort SAP Goods Receipt posting) API tests.

Vendor: vendor1@testco.com / Passw0rd123 (vendor_code S9999, approved).
Internal admin: synthetic vms_session with allowed_pages=['supplier_portal_admin'].
SAP endpoints are intentionally blank -> live_sync:false / sap_sync_status:'pending'
are EXPECTED states, asserted as such.
"""
import os
import uuid

import pytest
import requests
from dotenv import dotenv_values

frontend_env = dotenv_values("/app/frontend/.env")
base_url = os.environ.get("REACT_APP_BACKEND_URL") or frontend_env.get("REACT_APP_BACKEND_URL")
if not base_url:
    raise RuntimeError("REACT_APP_BACKEND_URL missing")
BASE_URL = base_url.rstrip("/")
API = f"{BASE_URL}/api"
DOMAIN = BASE_URL.split("//")[1]

ADMIN_SESSION_COOKIE = "_nNTeagpGyLWuMhFLKHF2qvr0lfmKm8P"
VENDOR_EMAIL = "vendor1@testco.com"
VENDOR_PASSWORD = "Passw0rd123"
PO_NUMBER = "PO4500012345"


def _db():
    import sys
    sys.path.insert(0, "/app/backend")
    from pymongo import MongoClient
    env = dotenv_values("/app/backend/.env")
    client = MongoClient(env["MONGO_URL"])
    return client, client[env["DB_NAME"]]


@pytest.fixture(scope="module")
def vendor_client():
    s = requests.Session()
    r = s.post(f"{API}/supplier-portal/login", json={"email": VENDOR_EMAIL, "password": VENDOR_PASSWORD})
    if r.status_code != 200:
        pytest.fail(f"vendor login failed {r.status_code}: {r.text[:300]}")
    return s


@pytest.fixture(scope="module")
def admin_client():
    s = requests.Session()
    s.cookies.set("vms_session", ADMIN_SESSION_COOKIE, domain=DOMAIN)
    return s


@pytest.fixture(scope="module")
def noperm_client():
    """Internal Entra user with allowed_pages=[] (seeded for this test run)."""
    client, db = _db()
    uid = "test-tid:test-oid-noperm-grn"
    from datetime import datetime, timedelta
    db["auth_users"].update_one({"_id": uid}, {"$set": {
        "email": "noperm.grn.test@rampgroup.co.in", "name": "TEST NoPerm",
        "role": "user", "allowed_pages": [], "bound_sites": []}}, upsert=True)
    sid = "TESTNOPERMGRNSESSION0000000000AA"
    db["auth_sessions"].update_one({"_id": sid}, {"$set": {
        "user_id": uid, "expires_at": datetime.utcnow() + timedelta(days=2)}}, upsert=True)
    client.close()
    s = requests.Session()
    s.cookies.set("vms_session", sid, domain=DOMAIN)
    return s


@pytest.fixture(scope="module", autouse=True)
def seed_s9999_po_cache():
    """Aug 28 2026: the new background loop (refresh_all_vendor_caches) deletes
    any cached row for a vendor that is not in SAP's current fetch window, which
    wipes the S9999 dummy fixture this suite relies on. Re-seed it here so the
    suite is self-contained (it may still be wiped by the loop mid-run - see
    test report)."""
    from datetime import datetime, timezone
    client, db = _db()
    now = datetime.now(timezone.utc)
    rows = [
        {"_id": "S9999::PO4500012345::10", "vendor_code": "S9999", "po_number": PO_NUMBER,
         "item_number": "10", "product_id": "TEST_P1", "description": "TEST_ Bolt M10",
         "po_qty": 5000, "unit_of_measure": "EA", "due_date": "2026-09-30", "updated_at": now},
        {"_id": "S9999::PO4500012345::20", "vendor_code": "S9999", "po_number": PO_NUMBER,
         "item_number": "20", "product_id": "TEST_P2", "description": "TEST_ Steel Coil",
         "po_qty": 1200, "unit_of_measure": "KGM", "due_date": "2026-09-30", "updated_at": now},
    ]
    for r in rows:
        db["supplier_portal_po_cache"].update_one({"_id": r["_id"]}, {"$set": r}, upsert=True)
    client.close()
    yield


@pytest.fixture(scope="module")
def created_codes():
    return []


@pytest.fixture(scope="module", autouse=True)
def cleanup(created_codes):
    yield
    if not created_codes:
        return
    try:
        client, db = _db()
        db["supplier_portal_shipments"].delete_many({"_id": {"$in": created_codes}})
        client.close()
    except Exception as e:  # pragma: no cover
        print(f"cleanup failed: {e}")


def _create_shipment(vendor_client, item_number, qty, created_codes=None):
    r = vendor_client.post(f"{API}/supplier-portal/shipments", json={
        "po_number": PO_NUMBER, "items": [{"item_number": item_number, "ship_qty": qty}]})
    if r.status_code == 200 and created_codes is not None:
        created_codes.append(r.json()["_id"])
    return r


def _get_po_item(vendor_client, item_number):
    r = vendor_client.get(f"{API}/supplier-portal/purchase-orders")
    assert r.status_code == 200, r.text
    for it in r.json()["purchase_orders"]:
        if it["po_number"] == PO_NUMBER and it["item_number"] == item_number:
            return it
    pytest.fail(f"item {item_number} missing from PO list")


# ---------- GET /api/supplier-portal/purchase-orders (cached fallback) ----------
class TestPurchaseOrderList:
    def test_po_list_returns_seeded_items_and_live_sync_false(self, vendor_client):
        r = vendor_client.get(f"{API}/supplier-portal/purchase-orders")
        assert r.status_code == 200, r.text
        data = r.json()
        assert "purchase_orders" in data and "live_sync" in data
        # live_sync now = "sap_po_watermark exists" (background loop), so True on a live tenant
        assert isinstance(data["live_sync"], bool)
        items = {i["item_number"]: i for i in data["purchase_orders"] if i["po_number"] == PO_NUMBER}
        assert {"10", "20"}.issubset(set(items)), items.keys()
        assert items["10"]["po_qty"] == 5000
        assert items["10"]["unit_of_measure"] == "EA"
        assert items["20"]["po_qty"] == 1200
        assert items["20"]["unit_of_measure"] == "KGM"
        for it in items.values():
            assert "remaining_qty" in it and "already_shipped_qty" in it
            assert it["remaining_qty"] == round(it["po_qty"] - it["already_shipped_qty"], 4)
            assert "_id" not in it

    def test_po_list_requires_login(self):
        r = requests.get(f"{API}/supplier-portal/purchase-orders")
        assert r.status_code == 401, r.text


# ---------- POST /api/supplier-portal/shipments (2-way match) ----------
class TestShipmentCreation:
    def test_create_shipment_returns_6char_code_and_persists(self, vendor_client, created_codes):
        before = _get_po_item(vendor_client, "10")["remaining_qty"]
        r = _create_shipment(vendor_client, "10", 10, created_codes)
        assert r.status_code == 200, r.text
        doc = r.json()
        code = doc["_id"]
        assert len(code) == 6 and code.isalnum() and code.upper() == code
        assert doc["status"] == "in_transit"
        assert doc["po_number"] == PO_NUMBER
        assert doc["vendor_code"] == "S9999"
        assert doc["items"][0]["ship_qty"] == 10
        assert doc["items"][0]["unit_of_measure"] == "EA"

        # verify in vendor's shipment list
        lr = vendor_client.get(f"{API}/supplier-portal/shipments")
        assert lr.status_code == 200
        found = [s for s in lr.json()["shipments"] if s["_id"] == code]
        assert found and found[0]["status"] == "in_transit"

        # remaining qty reduced
        assert _get_po_item(vendor_client, "10")["remaining_qty"] == before - 10

    def test_ship_qty_zero_rejected(self, vendor_client):
        r = _create_shipment(vendor_client, "10", 0)
        assert r.status_code == 400, r.text
        assert "greater than 0" in r.json()["detail"]

    def test_ship_qty_negative_rejected(self, vendor_client):
        r = _create_shipment(vendor_client, "10", -5)
        assert r.status_code == 400, r.text
        assert "greater than 0" in r.json()["detail"]

    def test_ship_more_than_po_qty_rejected(self, vendor_client):
        r = _create_shipment(vendor_client, "10", 999999)
        assert r.status_code == 400, r.text
        assert "still open" in r.json()["detail"]

    def test_ship_exceeding_remaining_after_prior_shipments_rejected(self, vendor_client, created_codes):
        remaining = _get_po_item(vendor_client, "10")["remaining_qty"]
        r = _create_shipment(vendor_client, "10", remaining + 1, created_codes)
        assert r.status_code == 400, r.text
        assert "still open" in r.json()["detail"]
        # exact remaining should be accepted, then nothing more
        # (use a small qty instead to keep the pool usable for other tests)
        ok = _create_shipment(vendor_client, "10", 1, created_codes)
        assert ok.status_code == 200, ok.text

    def test_unknown_item_rejected(self, vendor_client):
        r = _create_shipment(vendor_client, "999", 1)
        assert r.status_code == 400, r.text
        assert "not found" in r.json()["detail"].lower()

    def test_empty_items_rejected(self, vendor_client):
        r = vendor_client.post(f"{API}/supplier-portal/shipments", json={"po_number": PO_NUMBER, "items": []})
        assert r.status_code == 400, r.text

    def test_unknown_po_rejected(self, vendor_client):
        r = vendor_client.post(f"{API}/supplier-portal/shipments", json={
            "po_number": f"PO{uuid.uuid4().hex[:8]}", "items": [{"item_number": "10", "ship_qty": 1}]})
        assert r.status_code == 400, r.text

    def test_shipment_creation_requires_login(self):
        r = requests.post(f"{API}/supplier-portal/shipments", json={
            "po_number": PO_NUMBER, "items": [{"item_number": "10", "ship_qty": 1}]})
        assert r.status_code == 401, r.text

    def test_doc_codes_are_unique(self, vendor_client, admin_client, created_codes):
        codes = []
        for _ in range(5):
            r = _create_shipment(vendor_client, "10", 1, created_codes)
            assert r.status_code == 200, r.text
            codes.append(r.json()["_id"])
        assert len(set(codes)) == len(codes), codes
        ar = admin_client.get(f"{API}/admin/grn/shipments")
        assert ar.status_code == 200, ar.text
        all_codes = [s["_id"] for s in ar.json()["shipments"]]
        assert len(all_codes) == len(set(all_codes)), "duplicate doc codes exist in DB"
        for c in codes:
            assert c in all_codes


# ---------- GRN lookup / approve / reject ----------
class TestGrnAdmin:
    def test_lookup_case_insensitive(self, vendor_client, admin_client, created_codes):
        code = _create_shipment(vendor_client, "10", 3, created_codes).json()["_id"]
        for variant in (code, code.lower(), f"  {code.lower()}  "):
            r = admin_client.get(f"{API}/admin/grn/lookup/{variant.strip()}")
            assert r.status_code == 200, f"{variant} -> {r.status_code} {r.text[:200]}"
            assert r.json()["_id"] == code
            assert r.json()["po_number"] == PO_NUMBER
            assert r.json()["company_name"] == "Test Vendor Co"

    def test_lookup_unknown_code_404(self, admin_client):
        r = admin_client.get(f"{API}/admin/grn/lookup/ZZZ999")
        assert r.status_code == 404, r.text

    def test_pending_list_filtered_by_status(self, admin_client, vendor_client, created_codes):
        code = _create_shipment(vendor_client, "10", 2, created_codes).json()["_id"]
        r = admin_client.get(f"{API}/admin/grn/shipments", params={"status": "in_transit"})
        assert r.status_code == 200, r.text
        ships = r.json()["shipments"]
        assert all(s["status"] == "in_transit" for s in ships)
        assert code in [s["_id"] for s in ships]

    def test_approve_never_blocked_by_missing_sap_config(self, vendor_client, admin_client, created_codes):
        code = _create_shipment(vendor_client, "10", 4, created_codes).json()["_id"]
        r = admin_client.post(f"{API}/admin/grn/{code}/approve")
        assert r.status_code == 200, r.text
        doc = r.json()
        assert doc["status"] == "approved"
        assert doc["sap_sync_status"] == "pending", doc.get("sap_sync_status")
        assert doc["sap_gr_result"]["ok"] is False
        assert doc["sap_gr_result"]["reason"], "expected a human-readable pending reason"
        assert doc["approved_by"]
        assert doc["approved_at"]
        # persisted
        g = admin_client.get(f"{API}/admin/grn/lookup/{code.lower()}")
        assert g.json()["status"] == "approved"
        assert g.json()["sap_sync_status"] == "pending"

    def test_approved_shipment_cannot_be_reprocessed(self, vendor_client, admin_client, created_codes):
        code = _create_shipment(vendor_client, "10", 2, created_codes).json()["_id"]
        assert admin_client.post(f"{API}/admin/grn/{code}/approve").status_code == 200
        r2 = admin_client.post(f"{API}/admin/grn/{code}/approve")
        assert r2.status_code == 400, r2.text
        assert "already approved" in r2.json()["detail"]
        r3 = admin_client.post(f"{API}/admin/grn/{code}/reject", json={"reason": "TEST_double"})
        assert r3.status_code == 400, r3.text

    def test_reject_stores_reason_and_releases_qty(self, vendor_client, admin_client, created_codes):
        before = _get_po_item(vendor_client, "20")["remaining_qty"]
        code = _create_shipment(vendor_client, "20", 100, created_codes).json()["_id"]
        assert _get_po_item(vendor_client, "20")["remaining_qty"] == before - 100
        r = admin_client.post(f"{API}/admin/grn/{code}/reject", json={"reason": "TEST_damaged crate"})
        assert r.status_code == 200, r.text
        doc = r.json()
        assert doc["status"] == "rejected"
        assert doc["rejection_reason"] == "TEST_damaged crate"
        assert doc["rejected_by"]
        # qty released back into the 2-way match pool
        assert _get_po_item(vendor_client, "20")["remaining_qty"] == before
        # vendor sees the rejection reason
        vs = vendor_client.get(f"{API}/supplier-portal/shipments").json()["shipments"]
        mine = [s for s in vs if s["_id"] == code][0]
        assert mine["status"] == "rejected"
        assert mine["rejection_reason"] == "TEST_damaged crate"

    def test_rejected_shipment_cannot_be_reprocessed(self, vendor_client, admin_client, created_codes):
        code = _create_shipment(vendor_client, "20", 5, created_codes).json()["_id"]
        assert admin_client.post(f"{API}/admin/grn/{code}/reject", json={"reason": "TEST_x"}).status_code == 200
        r = admin_client.post(f"{API}/admin/grn/{code}/approve")
        assert r.status_code == 400, r.text
        assert "already rejected" in r.json()["detail"]

    def test_approve_unknown_code_404(self, admin_client):
        r = admin_client.post(f"{API}/admin/grn/QQQ111/approve")
        assert r.status_code == 404, r.text


# ---------- permission gating ----------
class TestGrnPermissions:
    def test_grn_routes_require_login(self):
        for method, path in [("get", "/admin/grn/shipments"), ("get", "/admin/grn/lookup/ABC123"),
                             ("post", "/admin/grn/ABC123/approve"), ("post", "/admin/grn/ABC123/reject")]:
            r = getattr(requests, method)(f"{API}{path}", json={})
            assert r.status_code == 401, f"{path} -> {r.status_code}"

    def test_supplier_jwt_does_not_authenticate_grn_routes(self, vendor_client):
        r = vendor_client.get(f"{API}/admin/grn/shipments")
        assert r.status_code == 401, f"{r.status_code} {r.text[:200]}"

    def test_user_without_page_permission_gets_403(self, admin_client):
        """Same synthetic session only has supplier_portal_admin -> other pages 403,
        proving PAGE_ROUTE_RULES gating is active for this session."""
        r = admin_client.get(f"{API}/suppliers")
        assert r.status_code == 403, r.text

    def test_internal_user_without_supplier_portal_admin_gets_403_on_grn(self, noperm_client):
        for method, path, body in [("get", "/admin/grn/shipments", None),
                                   ("get", "/admin/grn/lookup/ABC123", None),
                                   ("post", "/admin/grn/ABC123/approve", {}),
                                   ("post", "/admin/grn/ABC123/reject", {"reason": "x"})]:
            r = noperm_client.request(method, f"{API}{path}", json=body)
            assert r.status_code == 403, f"{path} -> {r.status_code} {r.text[:200]}"
        r = noperm_client.get(f"{API}/admin/supplier-portal/accounts")
        assert r.status_code == 403, r.text
