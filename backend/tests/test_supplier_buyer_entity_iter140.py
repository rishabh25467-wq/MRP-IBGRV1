"""Iteration 140 - Supplier Portal buyer-entity (RI/RT) scoping.

Covers: PO list buyer_code/buyer_entity_name, GRN lookup `allowed_site_ids`
narrowing, and backend rejection (HTTP 400) of an approve whose site_id does
not belong to the shipment's buyer entity.
"""
import os
from datetime import datetime, timezone

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
ADMIN_SESSION = "XAlQkzY5hG8PB3MdokeHOlLvQVFkAQuFFqwSgDD6Dpk"  # supplier.approver.test, bound P1/P2
TEST_RI_SHIPMENT = "ZZTEST140RI"
TEST_RT_SHIPMENT = "ZZTEST140RT"
SHIPMENTS_COLL = "supplier_portal_shipments"


@pytest.fixture(scope="module")
def db():
    client = MongoClient(BACK_ENV["MONGO_URL"])
    yield client[BACK_ENV["DB_NAME"]]
    client.close()


@pytest.fixture(scope="module")
def vendor_client():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/supplier-portal/login", json={"email": VENDOR_EMAIL, "password": VENDOR_PASSWORD}, timeout=60)
    if r.status_code != 200:
        pytest.fail(f"Vendor login failed {r.status_code}: {r.text[:300]}")
    return s


@pytest.fixture(scope="module")
def admin_client():
    s = requests.Session()
    s.cookies.set("vms_session", ADMIN_SESSION)
    return s


@pytest.fixture(scope="module", autouse=True)
def seeded_shipments(db):
    """Two synthetic shipments (RI + RT buyer_code) for GRN lookup/approve validation."""
    now = datetime.now(timezone.utc)

    def doc(code, buyer):
        return {
            "_id": code, "account_id": "zz-test-140", "vendor_code": VENDOR_CODE,
            "company_name": "TEST_ENTITY_SCOPE_CO", "status": "in_transit",
            "created_at": now, "updated_at": now,
            "items": [{
                "po_number": "ZZTESTPO140", "item_number": "10", "product_id": "ZZNOTREAL140",
                "description": "TEST_ item", "po_qty": 10, "already_shipped_qty": 0,
                "ship_qty": 5, "unit_of_measure": "EA", "buyer_code": buyer,
            }],
        }

    # idempotent seeding (safe under xdist re-runs)
    db[SHIPMENTS_COLL].replace_one({"_id": TEST_RI_SHIPMENT}, doc(TEST_RI_SHIPMENT, "RI"), upsert=True)
    db[SHIPMENTS_COLL].replace_one({"_id": TEST_RT_SHIPMENT}, doc(TEST_RT_SHIPMENT, "RT"), upsert=True)
    yield
    db[SHIPMENTS_COLL].delete_many({"_id": {"$in": [TEST_RI_SHIPMENT, TEST_RT_SHIPMENT]}})


# --- Supplier PO list: buyer entity fields present -------------------------
class TestSupplierPoBuyerEntity:
    def test_po_list_carries_buyer_code_and_entity_name(self, vendor_client):
        r = vendor_client.get(f"{BASE_URL}/supplier-portal/purchase-orders", timeout=120)
        assert r.status_code == 200, r.text[:300]
        data = r.json()
        pos = data.get("purchase_orders") or data.get("pos") or []
        assert len(pos) > 0, f"No POs returned for {VENDOR_CODE}: {list(data.keys())}"
        for po in pos:
            assert po.get("buyer_code") in ("RI", "RT"), po
            assert isinstance(po.get("buyer_entity_name"), str) and po["buyer_entity_name"]
        codes = {po["buyer_code"] for po in pos}
        assert codes == {"RI"}, f"H1330 expected RI-only POs, got {codes}"
        names = {po["buyer_entity_name"] for po in pos}
        assert names == {"RAY INTERNATIONAL"}, names


# --- GRN lookup: allowed_site_ids -----------------------------------------
class TestGrnLookupAllowedSites:
    def test_ri_shipment_allowed_sites(self, admin_client):
        r = admin_client.get(f"{BASE_URL}/admin/grn/lookup/{TEST_RI_SHIPMENT}", timeout=60)
        assert r.status_code == 200, r.text[:300]
        d = r.json()
        assert d["buyer_code"] == "RI"
        assert d["buyer_entity_name"] == "RAY INTERNATIONAL"
        allowed = d["allowed_site_ids"]
        # RI sites = P1, P8; this user is bound to P1/P2 -> narrows to P1 only
        assert allowed == ["P1"], allowed

    def test_rt_shipment_allowed_sites(self, admin_client):
        r = admin_client.get(f"{BASE_URL}/admin/grn/lookup/{TEST_RT_SHIPMENT}", timeout=60)
        assert r.status_code == 200, r.text[:300]
        d = r.json()
        assert d["buyer_code"] == "RT"
        assert d["buyer_entity_name"] == "RADISH TECHNOLOGIES"
        allowed = d["allowed_site_ids"]
        assert "P1" not in allowed, allowed
        assert "P2" in allowed, allowed

    def test_lookup_unknown_code_404(self, admin_client):
        r = admin_client.get(f"{BASE_URL}/admin/grn/lookup/ZZNOPE140", timeout=60)
        assert r.status_code == 404
        assert "detail" in r.json()

    def test_lookup_requires_auth(self):
        r = requests.get(f"{BASE_URL}/admin/grn/lookup/{TEST_RI_SHIPMENT}", timeout=60)
        assert r.status_code in (401, 403), r.status_code


# --- Approve: backend entity/site enforcement -----------------------------
def _approve_payload(site_id, warehouse_id="P2-RM"):
    return {
        "supplier_doc_num": "TEST_DOC_140", "bill_date": "2026-07-01",
        "site_id": site_id, "warehouse_id": warehouse_id,
        "item_actual_qtys": [{"po_number": "ZZTESTPO140", "item_number": "10", "actual_qty": 5}],
    }


class TestGrnApproveEntityEnforcement:
    def test_ri_shipment_rejects_rt_site(self, admin_client, db):
        r = admin_client.post(f"{BASE_URL}/admin/grn/{TEST_RI_SHIPMENT}/approve",
                              json=_approve_payload("P2"), timeout=60)
        assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text[:400]}"
        detail = r.json().get("detail", "")
        assert "RAY INTERNATIONAL" in detail, detail
        assert "P1" in detail and "P8" in detail, detail
        # not approved / unchanged in DB
        assert db[SHIPMENTS_COLL].find_one({"_id": TEST_RI_SHIPMENT})["status"] == "in_transit"

    def test_rt_shipment_rejects_ri_site(self, admin_client, db):
        r = admin_client.post(f"{BASE_URL}/admin/grn/{TEST_RT_SHIPMENT}/approve",
                              json=_approve_payload("P1", "P1-RM"), timeout=60)
        assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text[:400]}"
        detail = r.json().get("detail", "")
        assert "RADISH TECHNOLOGIES" in detail, detail
        assert db[SHIPMENTS_COLL].find_one({"_id": TEST_RT_SHIPMENT})["status"] == "in_transit"

    def test_unbound_site_still_403(self, admin_client):
        r = admin_client.post(f"{BASE_URL}/admin/grn/{TEST_RI_SHIPMENT}/approve",
                              json=_approve_payload("P8", "P8-RM"), timeout=60)
        assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text[:300]}"


# --- Legacy shipments (items without buyer_code) --------------------------
class TestLegacyShipmentFallback:
    def test_legacy_shipment_falls_back_to_all_sites(self, admin_client, db):
        legacy = db[SHIPMENTS_COLL].find_one(
            {"status": "in_transit", "items.buyer_code": {"$exists": False}})
        if not legacy:
            pytest.skip("No legacy (pre-buyer_code) in_transit shipment available")
        r = admin_client.get(f"{BASE_URL}/admin/grn/lookup/{legacy['_id']}", timeout=60)
        assert r.status_code == 200, r.text[:300]
        d = r.json()
        assert d.get("buyer_code") is None
        assert sorted(d["allowed_site_ids"]) == ["P1", "P2"], d["allowed_site_ids"]


# --- Warehouse list (QC preselect source data) ----------------------------
class TestWarehouses:
    def test_p1_warehouses_include_qc(self, admin_client):
        r = admin_client.get(f"{BASE_URL}/admin/grn/warehouses/P1", timeout=120)
        assert r.status_code == 200, r.text[:300]
        whs = [w["warehouse_id"] for w in r.json().get("warehouses", [])]
        assert whs, "no warehouses returned for P1"
        print("P1 warehouses:", whs)
        assert any(w.split("-")[-1] == "QC" for w in whs), whs
