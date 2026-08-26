"""Session Aug 28 2026 fix verification: supplier-portal PO list is now
served ONLY from the Mongo cache (supplier_portal_po_cache) populated by
a background loop that does one global SAP fetch with a watermark-bounded
SelectionByID query. Tests:
  - vendor isolation (H1330 sees only its own POs)
  - data currency (2026 due dates, PO IDs near the watermark, not 2024/39xx)
  - live_sync flag
  - empty-but-graceful list for the dummy S9999 vendor
  - shipment creation 2-way match on the new cache source
"""
import os
from datetime import date

import pytest
import requests
from dotenv import dotenv_values

_env = dotenv_values("/app/frontend/.env")
BASE_URL = (os.environ.get("REACT_APP_BACKEND_URL") or _env.get("REACT_APP_BACKEND_URL")).rstrip("/")
API = f"{BASE_URL}/api"

HAMIDI = {"email": "hamidi.demo@vendorportal.test", "password": "HamidiDemo123"}
DUMMY = {"email": "vendor1@testco.com", "password": "Passw0rd123"}

_be = dotenv_values("/app/backend/.env")


@pytest.fixture(scope="module")
def mongo_db():
    from pymongo import MongoClient
    client = MongoClient(_be["MONGO_URL"])
    yield client[_be["DB_NAME"]]
    client.close()


def _login(creds):
    s = requests.Session()
    r = s.post(f"{API}/supplier-portal/login", json=creds, timeout=60)
    assert r.status_code == 200, f"login failed {r.status_code}: {r.text[:300]}"
    return s


@pytest.fixture(scope="module")
def hamidi_client():
    return _login(HAMIDI)


@pytest.fixture(scope="module")
def dummy_client():
    return _login(DUMMY)


@pytest.fixture(scope="module")
def created_shipment_codes():
    return []


@pytest.fixture(scope="module", autouse=True)
def cleanup(mongo_db, created_shipment_codes):
    yield
    for code in created_shipment_codes:
        mongo_db["supplier_portal_shipments"].delete_one({"_id": code})


# --- module: GET /api/supplier-portal/purchase-orders (cache-only read) ---
class TestVendorPOList:
    def test_response_is_fast_and_well_formed(self, hamidi_client):
        r = hamidi_client.get(f"{API}/supplier-portal/purchase-orders", timeout=60)
        assert r.status_code == 200, r.text[:400]
        assert r.elapsed.total_seconds() < 10, f"cache read took {r.elapsed.total_seconds()}s"
        data = r.json()
        assert set(["purchase_orders", "live_sync"]).issubset(data.keys())
        assert data["live_sync"] is True
        assert isinstance(data["purchase_orders"], list)

    def test_hamidi_sees_only_own_current_pos(self, hamidi_client):
        rows = hamidi_client.get(f"{API}/supplier-portal/purchase-orders", timeout=60).json()["purchase_orders"]
        assert len(rows) > 0, "Hamidi (H1330) should have cached open POs"
        pos = sorted({r["po_number"] for r in rows})
        assert pos == ["28792", "28833", "28897"], f"unexpected PO set: {pos}"
        assert len(rows) == 10, f"expected 10 line items, got {len(rows)}"
        for r in rows:
            assert r["vendor_code"] == "H1330"
            # currency check - no stale 2024 window data
            assert int(r["po_number"]) > 20000, f"stale PO id {r['po_number']}"
            assert r["due_date"] and r["due_date"] >= "2026-01-01", f"stale due date {r['due_date']}"
            assert r["po_qty"] > 0
            assert r["unit_of_measure"]
            assert "remaining_qty" in r and "already_shipped_qty" in r
            assert "_id" not in r

    def test_no_mongo_objectid_or_other_vendor_leak(self, hamidi_client, mongo_db):
        rows = hamidi_client.get(f"{API}/supplier-portal/purchase-orders", timeout=60).json()["purchase_orders"]
        other_vendors = {c for c in mongo_db["supplier_portal_po_cache"].distinct("vendor_code") if c != "H1330"}
        assert len(other_vendors) > 5, "cache should hold many vendors (global fetch)"
        assert not ({r["vendor_code"] for r in rows} & other_vendors)

    def test_dummy_vendor_gets_graceful_empty_list(self, dummy_client):
        r = dummy_client.get(f"{API}/supplier-portal/purchase-orders", timeout=60)
        assert r.status_code == 200, r.text[:400]
        data = r.json()
        assert data["purchase_orders"] == []
        assert data["live_sync"] is True

    def test_unauthenticated_gets_401(self):
        r = requests.get(f"{API}/supplier-portal/purchase-orders", timeout=60)
        assert r.status_code == 401


# --- module: sap_po_watermark (recency window state) ---
class TestWatermark:
    def test_watermark_tracks_current_max(self, mongo_db):
        wm = mongo_db["sap_po_watermark"].find_one({"_id": "latest"})
        assert wm is not None
        assert wm["max_po_id"] > 20000, f"watermark stuck at old value: {wm['max_po_id']}"
        assert wm["updated_at"] is not None

    def test_cache_window_is_recent(self, mongo_db):
        ids = [int(r["po_number"]) for r in mongo_db["supplier_portal_po_cache"].find({}, {"po_number": 1})
               if str(r["po_number"]).isdigit()]
        assert ids, "cache is empty"
        assert min(ids) > 20000, f"cache contains stale (oldest-first bug) PO ids: min={min(ids)}"


# --- module: POST /api/supplier-portal/shipments (2-way match on new cache) ---
class TestShipmentOnNewCache:
    def test_create_shipment_and_validate(self, hamidi_client, created_shipment_codes):
        rows = hamidi_client.get(f"{API}/supplier-portal/purchase-orders", timeout=60).json()["purchase_orders"]
        target = next(r for r in rows if r["remaining_qty"] > 5)
        po, item, remaining = target["po_number"], target["item_number"], target["remaining_qty"]

        # over-ship must be rejected
        over = hamidi_client.post(f"{API}/supplier-portal/shipments", json={
            "po_number": po, "items": [{"item_number": item, "ship_qty": remaining + 100}]}, timeout=60)
        assert over.status_code == 400, over.text[:300]
        assert "only" in over.json()["detail"].lower()

        # zero qty rejected
        zero = hamidi_client.post(f"{API}/supplier-portal/shipments", json={
            "po_number": po, "items": [{"item_number": item, "ship_qty": 0}]}, timeout=60)
        assert zero.status_code == 400

        # valid partial ship
        ok = hamidi_client.post(f"{API}/supplier-portal/shipments", json={
            "po_number": po, "items": [{"item_number": item, "ship_qty": 5}]}, timeout=60)
        assert ok.status_code == 200, ok.text[:400]
        doc = ok.json()
        code = doc.get("doc_code") or doc.get("_id") or doc.get("id")
        assert code and len(code) == 6 and code.isalnum() and code == code.upper()
        created_shipment_codes.append(code)
        assert doc["status"] == "in_transit"
        assert doc["po_number"] == po

        # remaining qty reflects the shipment
        rows2 = hamidi_client.get(f"{API}/supplier-portal/purchase-orders", timeout=60).json()["purchase_orders"]
        after = next(r for r in rows2 if r["po_number"] == po and r["item_number"] == item)
        assert after["already_shipped_qty"] >= 5
        assert abs(after["remaining_qty"] - (remaining - 5)) < 1e-6

    def test_unknown_item_rejected(self, hamidi_client):
        rows = hamidi_client.get(f"{API}/supplier-portal/purchase-orders", timeout=60).json()["purchase_orders"]
        po = rows[0]["po_number"]
        r = hamidi_client.post(f"{API}/supplier-portal/shipments", json={
            "po_number": po, "items": [{"item_number": "9999", "ship_qty": 1}]}, timeout=60)
        assert r.status_code == 400
        assert "not found" in r.json()["detail"].lower()

    def test_dummy_vendor_cannot_ship_hamidi_po(self, dummy_client):
        r = dummy_client.post(f"{API}/supplier-portal/shipments", json={
            "po_number": "28792", "items": [{"item_number": "1", "ship_qty": 1}]}, timeout=60)
        assert r.status_code == 400, r.text[:300]

    def test_doc_codes_unique(self, hamidi_client, created_shipment_codes):
        rows = hamidi_client.get(f"{API}/supplier-portal/purchase-orders", timeout=60).json()["purchase_orders"]
        target = next(r for r in rows if r["remaining_qty"] > 3)
        codes = set()
        for _ in range(2):
            r = hamidi_client.post(f"{API}/supplier-portal/shipments", json={
                "po_number": target["po_number"],
                "items": [{"item_number": target["item_number"], "ship_qty": 1}]}, timeout=60)
            assert r.status_code == 200, r.text[:300]
            c = r.json().get("doc_code") or r.json().get("_id")
            codes.add(c)
            created_shipment_codes.append(c)
        assert len(codes) == 2


# --- module: regression smoke on untouched internal endpoints ---
@pytest.mark.parametrize("path", [
    "/health",
    "/inventory/summary",
    "/suppliers",
    "/products",
])
def test_internal_endpoints_reachable(path):
    r = requests.get(f"{API}{path}", timeout=120)
    assert r.status_code in (200, 401, 403, 404), f"{path} -> {r.status_code}: {r.text[:200]}"
