"""Iteration 141 - regression of iteration_140 fixes:
(1) backend rejection of MIXED buyer-entity (RI+RT) shipments on create AND edit,
(2) GRN lookup `site_access_blocked` flag,
(3) site narrowing / QC warehouse still intact after the visual refresh.

Fixtures seeded/removed inside the module: one temp `supplier_portal_po_cache`
row with buyer_code='RT' for vendor H1330, synthetic auth sessions, and any
shipment created by the mixed-entity tests.

RUN WITH `-n 0` (pytest.ini enables xdist by default): the module-scoped
fixtures seed/teardown shared Mongo rows, so parallel workers tear each
other's fixtures down mid-run.
"""
import os
import secrets
from datetime import datetime, timedelta, timezone

import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient

frontend_env = dotenv_values("/app/frontend/.env")
base_url = os.environ.get("REACT_APP_BACKEND_URL") or frontend_env.get("REACT_APP_BACKEND_URL")
if not base_url:
    raise RuntimeError("REACT_APP_BACKEND_URL missing")
BASE_URL = base_url.rstrip("/") + "/api"

BACK_ENV = dotenv_values("/app/backend/.env")
VENDOR_EMAIL = "hamidi.demo@vendorportal.test"
VENDOR_PASSWORD = "HamidiDemo123"
VENDOR_CODE = "H1330"
SHIPMENTS_COLL = "supplier_portal_shipments"
PO_CACHE_COLL = "supplier_portal_po_cache"

RT_PO = "ZZRT141"
RT_ITEM = "1"
RT_CACHE_ID = f"{VENDOR_CODE}::{RT_PO}::{RT_ITEM}"
RI_SHIP = "ZZ141R"   # <=6 chars (GRN input maxLength=6)
BLOCKED_SESSION = "zz141sessionblockedp3"
SUPER_SESSION = "zz141sessionsuperadmin"


@pytest.fixture(scope="module")
def db():
    client = MongoClient(BACK_ENV["MONGO_URL"])
    yield client[BACK_ENV["DB_NAME"]]
    client.close()


@pytest.fixture(scope="module")
def vendor_client():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/supplier-portal/login",
               json={"email": VENDOR_EMAIL, "password": VENDOR_PASSWORD}, timeout=60)
    if r.status_code != 200:
        pytest.fail(f"Vendor login failed {r.status_code}: {r.text[:300]}")
    return s


@pytest.fixture(scope="module")
def ri_item(vendor_client):
    """A real RI PO line item for this vendor (from the live PO list)."""
    r = vendor_client.get(f"{BASE_URL}/supplier-portal/purchase-orders", timeout=180)
    assert r.status_code == 200, r.text[:300]
    pos = r.json().get("purchase_orders") or []
    ri = [p for p in pos if p.get("buyer_code") == "RI" and (p.get("remaining_qty") or 0) > 0]
    if not ri:
        pytest.fail("No open RI PO line available for H1330")
    return ri[0]


@pytest.fixture(scope="module", autouse=True)
def fixtures(db):
    now = datetime.now(timezone.utc)
    # temp RT po_cache row (no `source` field -> untouched by the background refresh loop)
    db[PO_CACHE_COLL].replace_one({"_id": RT_CACHE_ID}, {
        "_id": RT_CACHE_ID, "vendor_code": VENDOR_CODE, "vendor_name": "HAMIDI EXPORTS",
        "po_number": RT_PO, "item_number": RT_ITEM, "product_id": "ZZNOTREAL141",
        "description": "TEST_ RT entity line", "po_qty": 20, "unit_of_measure": "EA",
        "buyer_code": "RT", "updated_at": now, "expired": False,
    }, upsert=True)
    # synthetic sessions
    for sid, uid, email, role, bound in (
        (BLOCKED_SESSION, "zz141-tid:blocked-oid", "zz141.blocked@internal.test", "user", ["P3"]),
        (SUPER_SESSION, "zz141-tid:super-oid", "zz141.super@internal.test", "super_admin", []),
    ):
        db["auth_users"].update_one({"_id": uid}, {"$set": {
            "tid": "zz141-tid", "oid": uid.split(":")[1], "email": email, "name": "ZZ141 Tester",
            "role": role, "allowed_pages": ["supplier_portal_admin"], "bound_sites": bound,
        }}, upsert=True)
        db["auth_sessions"].replace_one({"_id": sid}, {
            "_id": sid, "user_id": uid, "expires_at": now + timedelta(days=1),
        }, upsert=True)
    yield
    db[PO_CACHE_COLL].delete_one({"_id": RT_CACHE_ID})
    db["auth_sessions"].delete_many({"_id": {"$in": [BLOCKED_SESSION, SUPER_SESSION]}})
    db["auth_users"].delete_many({"_id": {"$in": ["zz141-tid:blocked-oid", "zz141-tid:super-oid"]}})
    db[SHIPMENTS_COLL].delete_many({"items.po_number": RT_PO})
    db[SHIPMENTS_COLL].delete_many({"_id": RI_SHIP})
    db[SHIPMENTS_COLL].delete_many({"company_name": "TEST_ENTITY_141"})


def _admin(session_id):
    s = requests.Session()
    s.cookies.set("vms_session", session_id)
    return s


# --- iteration_140 fix #1: mixed-entity shipments rejected ------------------
class TestMixedEntityRejection:
    def test_create_mixed_entity_rejected(self, vendor_client, ri_item, db):
        payload = {"items": [
            {"po_number": ri_item["po_number"], "item_number": ri_item["item_number"], "ship_qty": 1},
            {"po_number": RT_PO, "item_number": RT_ITEM, "ship_qty": 1},
        ]}
        r = vendor_client.post(f"{BASE_URL}/supplier-portal/shipments", json=payload, timeout=90)
        assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text[:400]}"
        detail = r.json().get("detail", "")
        assert "RAY INTERNATIONAL" in detail, detail
        assert "RADISH TECHNOLOGIES" in detail, detail
        # nothing persisted
        assert db[SHIPMENTS_COLL].count_documents({"items.po_number": RT_PO}) == 0

    def test_single_entity_create_still_works_and_edit_to_mixed_rejected(self, vendor_client, ri_item, db):
        # CREATE - RI only, must succeed
        create = vendor_client.post(f"{BASE_URL}/supplier-portal/shipments", json={"items": [
            {"po_number": ri_item["po_number"], "item_number": ri_item["item_number"], "ship_qty": 1},
        ]}, timeout=90)
        assert create.status_code == 200, create.text[:400]
        doc = create.json()
        code = doc["_id"] if "_id" in doc else doc["doc_code"]
        try:
            assert len(code) == 6
            assert doc["status"] == "in_transit"
            assert doc["items"][0]["buyer_code"] == "RI"
            # GET verifies persistence
            lst = vendor_client.get(f"{BASE_URL}/supplier-portal/shipments", timeout=90)
            assert lst.status_code == 200
            assert code in [s["_id"] for s in lst.json()["shipments"]]

            # EDIT to add an RT line -> must be rejected
            r = vendor_client.put(f"{BASE_URL}/supplier-portal/shipments/{code}", json={"items": [
                {"po_number": ri_item["po_number"], "item_number": ri_item["item_number"], "ship_qty": 1},
                {"po_number": RT_PO, "item_number": RT_ITEM, "ship_qty": 1},
            ]}, timeout=90)
            assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text[:400]}"
            detail = r.json().get("detail", "")
            assert "RAY INTERNATIONAL" in detail and "RADISH TECHNOLOGIES" in detail, detail
            # unchanged in DB - still the single RI line
            stored = db[SHIPMENTS_COLL].find_one({"_id": code})
            assert len(stored["items"]) == 1, stored["items"]
            assert stored["items"][0]["po_number"] == ri_item["po_number"]

            # legit edit (qty change) still works
            r2 = vendor_client.put(f"{BASE_URL}/supplier-portal/shipments/{code}", json={"items": [
                {"po_number": ri_item["po_number"], "item_number": ri_item["item_number"], "ship_qty": 2},
            ]}, timeout=90)
            assert r2.status_code == 200, r2.text[:400]
            assert db[SHIPMENTS_COLL].find_one({"_id": code})["items"][0]["ship_qty"] == 2
        finally:
            db[SHIPMENTS_COLL].delete_one({"_id": code})

    def test_rt_only_create_allowed(self, vendor_client, db):
        r = vendor_client.post(f"{BASE_URL}/supplier-portal/shipments", json={"items": [
            {"po_number": RT_PO, "item_number": RT_ITEM, "ship_qty": 1},
        ]}, timeout=90)
        assert r.status_code == 200, r.text[:400]
        code = r.json()["_id"]
        try:
            assert r.json()["items"][0]["buyer_code"] == "RT"
        finally:
            db[SHIPMENTS_COLL].delete_one({"_id": code})


# --- iteration_140 fix #2: site_access_blocked flag ------------------------
class TestSiteAccessBlocked:
    @pytest.fixture(scope="class", autouse=True)
    def ri_shipment(self, db):
        now = datetime.now(timezone.utc)
        db[SHIPMENTS_COLL].replace_one({"_id": RI_SHIP}, {
            "_id": RI_SHIP, "account_id": "zz-141", "vendor_code": VENDOR_CODE,
            "company_name": "TEST_ENTITY_141", "status": "in_transit",
            "created_at": now, "updated_at": now,
            "items": [{"po_number": "ZZPO141", "item_number": "10", "product_id": "ZZNOTREAL141",
                       "description": "TEST_ item", "po_qty": 10, "already_shipped_qty": 0,
                       "ship_qty": 5, "unit_of_measure": "EA", "buyer_code": "RI"}],
        }, upsert=True)
        yield
        db[SHIPMENTS_COLL].delete_one({"_id": RI_SHIP})

    def test_blocked_user_gets_flag_true_and_empty_sites(self):
        r = _admin(BLOCKED_SESSION).get(f"{BASE_URL}/admin/grn/lookup/{RI_SHIP}", timeout=60)
        assert r.status_code == 200, r.text[:300]
        d = r.json()
        assert d["buyer_code"] == "RI"
        assert d["buyer_entity_name"] == "RAY INTERNATIONAL"
        assert d["allowed_site_ids"] == [], d["allowed_site_ids"]
        assert d["site_access_blocked"] is True

    def test_super_admin_not_blocked_and_sees_only_ri_sites(self):
        r = _admin(SUPER_SESSION).get(f"{BASE_URL}/admin/grn/lookup/{RI_SHIP}", timeout=60)
        assert r.status_code == 200, r.text[:300]
        d = r.json()
        assert d["site_access_blocked"] is False
        assert sorted(d["allowed_site_ids"]) == ["P1", "P8"], d["allowed_site_ids"]

    def test_blocked_user_approve_still_rejected(self, db):
        r = _admin(BLOCKED_SESSION).post(f"{BASE_URL}/admin/grn/{RI_SHIP}/approve", json={
            "supplier_doc_num": "TEST_141", "bill_date": "2026-07-01",
            "site_id": "P3", "warehouse_id": "P3-RM",
            "item_actual_qtys": [{"po_number": "ZZPO141", "item_number": "10", "actual_qty": 5}],
        }, timeout=60)
        assert r.status_code in (400, 403), f"{r.status_code}: {r.text[:300]}"
        assert db[SHIPMENTS_COLL].find_one({"_id": RI_SHIP})["status"] == "in_transit"


# --- site narrowing + QC warehouse still intact ---------------------------
class TestSiteNarrowingAndWarehouses:
    @pytest.fixture(scope="class", autouse=True)
    def rt_shipment(self, db):
        code = "ZZ141T"
        now = datetime.now(timezone.utc)
        db[SHIPMENTS_COLL].replace_one({"_id": code}, {
            "_id": code, "account_id": "zz-141", "vendor_code": VENDOR_CODE,
            "company_name": "TEST_ENTITY_141", "status": "in_transit",
            "created_at": now, "updated_at": now,
            "items": [{"po_number": "ZZPO141T", "item_number": "10", "product_id": "ZZNOTREAL141",
                       "description": "TEST_ item", "po_qty": 10, "already_shipped_qty": 0,
                       "ship_qty": 5, "unit_of_measure": "EA", "buyer_code": "RT"}],
        }, upsert=True)
        yield code
        db[SHIPMENTS_COLL].delete_one({"_id": code})

    def test_rt_shipment_sites_exclude_ri_sites(self, rt_shipment):
        r = _admin(SUPER_SESSION).get(f"{BASE_URL}/admin/grn/lookup/ZZ141T", timeout=60)
        assert r.status_code == 200, r.text[:300]
        d = r.json()
        allowed = d["allowed_site_ids"]
        assert d["site_access_blocked"] is False
        assert "P1" not in allowed and "P8" not in allowed, allowed
        assert "P2" in allowed, allowed

    def test_p1_warehouses_include_qc(self):
        r = _admin(SUPER_SESSION).get(f"{BASE_URL}/admin/grn/warehouses/P1", timeout=180)
        assert r.status_code == 200, r.text[:300]
        whs = [w["warehouse_id"] for w in r.json().get("warehouses", [])]
        assert whs, "no warehouses returned for P1"
        assert any(w.endswith("-QC") for w in whs), whs

    def test_pending_shipments_list(self):
        r = _admin(SUPER_SESSION).get(f"{BASE_URL}/admin/grn/shipments?status=in_transit", timeout=60)
        assert r.status_code == 200, r.text[:300]
        ships = r.json().get("shipments", [])
        assert isinstance(ships, list)
        assert all("_id" in s for s in ships)

    def test_lookup_unknown_404(self):
        r = _admin(SUPER_SESSION).get(f"{BASE_URL}/admin/grn/lookup/ZZNOPE", timeout=60)
        assert r.status_code == 404

    def test_lookup_requires_auth(self):
        r = requests.get(f"{BASE_URL}/admin/grn/lookup/{RI_SHIP}", timeout=60)
        assert r.status_code in (401, 403), r.status_code
