"""Iteration 123 - Supplier Portal new features:
test@test.com login, testing-mode vendor impersonation, PO extra fields,
multi-PO cart shipment create, shipment edit (PUT), GRN regression.
"""
import os

import pytest
import requests
from dotenv import dotenv_values

frontend_env = dotenv_values("/app/frontend/.env")
BASE = (os.environ.get("REACT_APP_BACKEND_URL") or frontend_env.get("REACT_APP_BACKEND_URL")).rstrip("/")
API = f"{BASE}/api"

TEST_EMAIL = "test@test.com"
TEST_PASSWORD = "Test123"
DEMO_EMAIL = "hamidi.demo@vendorportal.test"
DEMO_PASSWORD = "HamidiDemo123"


@pytest.fixture(scope="module")
def session():
    s = requests.Session()
    r = s.post(f"{API}/supplier-portal/login", json={"email": TEST_EMAIL, "password": TEST_PASSWORD}, timeout=60)
    if r.status_code != 200:
        pytest.fail(f"login failed {r.status_code}: {r.text[:300]}")
    return s


@pytest.fixture(scope="module")
def created_codes():
    return []


# --- auth / me ---
class TestAuth:
    def test_new_test_login(self, session):
        r = session.get(f"{API}/supplier-portal/me", timeout=60)
        assert r.status_code == 200
        d = r.json()
        assert d.get("authenticated") is not False
        assert d["vendor_code"] == "H1330", d
        assert d.get("status") == "approved"
        assert d.get("testing_mode") is True, "testing_mode must be exposed as true"

    def test_demo_login_regression(self):
        s = requests.Session()
        r = s.post(f"{API}/supplier-portal/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD}, timeout=60)
        assert r.status_code == 200, r.text[:300]
        me = s.get(f"{API}/supplier-portal/me", timeout=60).json()
        assert me["vendor_code"] == "H1330"

    def test_bad_password_rejected(self):
        r = requests.post(f"{API}/supplier-portal/login", json={"email": TEST_EMAIL, "password": "wrong"}, timeout=60)
        assert r.status_code in (400, 401), r.status_code

    def test_pos_require_auth(self):
        r = requests.get(f"{API}/supplier-portal/purchase-orders", timeout=60)
        assert r.status_code == 401


# --- PO list new fields ---
class TestPurchaseOrders:
    def test_po_fields(self, session):
        r = session.get(f"{API}/supplier-portal/purchase-orders", timeout=120)
        assert r.status_code == 200, r.text[:400]
        d = r.json()
        assert d["vendor_code"] == "H1330"
        pos = d["purchase_orders"]
        assert len(pos) > 0, "no cached POs for H1330"
        nums = {p["po_number"] for p in pos}
        for expected in ("28792", "28833", "28897"):
            assert expected in nums, f"missing known PO {expected}: {sorted(nums)}"
        for p in pos:
            assert p.get("po_date"), f"missing po_date {p}"
            assert p.get("unit_price") is not None, f"missing unit_price {p}"
            assert p.get("subtotal") is not None, f"missing subtotal {p}"
            assert p.get("buyer_entity_name") == "RAY INTERNATIONAL", p.get("buyer_entity_name")
            assert p.get("remaining_qty") is not None
            assert "_id" not in p

    def test_live_sync_flag_present(self, session):
        d = session.get(f"{API}/supplier-portal/purchase-orders", timeout=120).json()
        assert "live_sync" in d and "last_synced_at" in d


# --- testing-only impersonation ---
class TestImpersonation:
    def test_vendor_directory_search(self, session):
        r = session.get(f"{API}/supplier-portal/testing/vendor-directory", params={"q": ""}, timeout=60)
        assert r.status_code == 200, r.text[:300]
        vendors = r.json()["vendors"]
        assert len(vendors) > 0
        assert all("vendor_code" in v for v in vendors)

    def test_vendor_directory_requires_auth(self):
        r = requests.get(f"{API}/supplier-portal/testing/vendor-directory", timeout=60)
        assert r.status_code == 401

    def test_as_vendor_override_changes_data(self, session):
        vendors = session.get(f"{API}/supplier-portal/testing/vendor-directory", timeout=60).json()["vendors"]
        other = next(v for v in vendors if v["vendor_code"] != "H1330")
        r = session.get(f"{API}/supplier-portal/purchase-orders", params={"as_vendor": other["vendor_code"]}, timeout=120)
        assert r.status_code == 200, r.text[:300]
        d = r.json()
        assert d["vendor_code"] == other["vendor_code"]
        own = session.get(f"{API}/supplier-portal/purchase-orders", timeout=120).json()["purchase_orders"]
        assert d["purchase_orders"] != own, "impersonation returned identical data"
        assert len(d["purchase_orders"]) > 0


# --- multi-PO cart shipment ---
class TestShipments:
    def test_create_multi_po_shipment(self, session, created_codes):
        pos = session.get(f"{API}/supplier-portal/purchase-orders", timeout=120).json()["purchase_orders"]
        by_po = {}
        for p in pos:
            if (p.get("remaining_qty") or 0) > 1:
                by_po.setdefault(p["po_number"], p)
        assert len(by_po) >= 2, "need 2 POs with open qty"
        picks = list(by_po.values())[:2]
        payload = {"items": [{"po_number": p["po_number"], "item_number": p["item_number"], "ship_qty": 1} for p in picks]}
        r = session.post(f"{API}/supplier-portal/shipments", json=payload, timeout=60)
        assert r.status_code == 200, r.text[:400]
        ship = r.json()
        code = ship["_id"]
        created_codes.append(code)
        assert len(code) == 6
        assert ship["status"] == "in_transit"
        assert "po_number" not in ship, "top-level po_number should be gone from schema"
        assert len({i["po_number"] for i in ship["items"]}) == 2
        # verify persistence via GET
        lst = session.get(f"{API}/supplier-portal/shipments", timeout=60).json()["shipments"]
        found = [s for s in lst if s["_id"] == code]
        assert found, "created shipment missing from list"
        assert len({i["po_number"] for i in found[0]["items"]}) == 2

    def test_over_ship_rejected(self, session):
        pos = session.get(f"{API}/supplier-portal/purchase-orders", timeout=120).json()["purchase_orders"]
        p = pos[0]
        r = session.post(f"{API}/supplier-portal/shipments", json={"items": [
            {"po_number": p["po_number"], "item_number": p["item_number"], "ship_qty": (p["remaining_qty"] or 0) + 1000}
        ]}, timeout=60)
        assert r.status_code == 400, r.status_code

    def test_empty_cart_rejected(self, session):
        r = session.post(f"{API}/supplier-portal/shipments", json={"items": []}, timeout=60)
        assert r.status_code == 400

    def test_unknown_item_rejected(self, session):
        r = session.post(f"{API}/supplier-portal/shipments", json={"items": [
            {"po_number": "99999999", "item_number": "1", "ship_qty": 1}
        ]}, timeout=60)
        assert r.status_code == 400

    def test_update_shipment_items(self, session, created_codes):
        assert created_codes, "no shipment created"
        code = created_codes[0]
        pos = session.get(f"{API}/supplier-portal/purchase-orders", timeout=120).json()["purchase_orders"]
        cur = [s for s in session.get(f"{API}/supplier-portal/shipments", timeout=60).json()["shipments"] if s["_id"] == code][0]
        keys = {(i["po_number"], i["item_number"]) for i in cur["items"]}
        extra = next((p for p in pos if (p["po_number"], p["item_number"]) not in keys and (p.get("remaining_qty") or 0) > 1), None)
        assert extra, "no extra item available to add"
        items = [{"po_number": i["po_number"], "item_number": i["item_number"], "ship_qty": 2} for i in cur["items"][:1]]
        items.append({"po_number": extra["po_number"], "item_number": extra["item_number"], "ship_qty": 3})
        r = session.put(f"{API}/supplier-portal/shipments/{code}", json={"items": items}, timeout=60)
        assert r.status_code == 200, r.text[:400]
        updated = r.json()
        assert len(updated["items"]) == 2
        assert any(i["ship_qty"] == 3 for i in updated["items"])
        # persistence
        fresh = [s for s in session.get(f"{API}/supplier-portal/shipments", timeout=60).json()["shipments"] if s["_id"] == code][0]
        assert len(fresh["items"]) == 2
        assert any(i["ship_qty"] == 3 for i in fresh["items"])
        assert any(i["ship_qty"] == 2 for i in fresh["items"])

    def test_update_unknown_code_404(self, session):
        r = session.put(f"{API}/supplier-portal/shipments/ZZZZZZ", json={"items": [
            {"po_number": "28792", "item_number": "2", "ship_qty": 1}
        ]}, timeout=60)
        assert r.status_code == 404, r.status_code

    def test_update_other_vendors_shipment_forbidden(self, session, created_codes):
        """A different account (demo) must not be able to edit test@test.com's shipment."""
        s2 = requests.Session()
        s2.post(f"{API}/supplier-portal/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD}, timeout=60)
        r = s2.put(f"{API}/supplier-portal/shipments/{created_codes[0]}", json={"items": [
            {"po_number": "28792", "item_number": "2", "ship_qty": 1}
        ]}, timeout=60)
        assert r.status_code == 404, f"expected 404 ownership guard, got {r.status_code}"


# --- GRN internal lookup regression (multi-PO schema) ---
class TestGrnLookup:
    def test_admin_lookup_shipment(self, session):
        # xdist loadscope runs this class in its own worker, so fetch a
        # shipment code from the API rather than relying on cross-class state.
        shipments = session.get(f"{API}/supplier-portal/shipments", timeout=60).json()["shipments"]
        assert shipments, "no shipments for H1330 to look up"
        code = shipments[0]["_id"]
        r = requests.get(f"{API}/admin/grn/lookup/{code}", timeout=60)
        assert r.status_code in (200, 401, 403), f"{r.status_code}: {r.text[:200]}"
        if r.status_code == 200:
            d = r.json()
            assert "items" in d
            assert all("po_number" in i for i in d["items"])
