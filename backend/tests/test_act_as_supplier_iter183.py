"""Iter183: Act as Supplier feature - backend API tests.

Endpoints under test (all gated by 'act_as_supplier' page permission via
auth_service.PAGE_ROUTE_RULES):
  GET  /api/admin/act-as-supplier/accounts
  GET  /api/admin/act-as-supplier/{account_id}/purchase-orders
  POST /api/admin/act-as-supplier/{account_id}/shipments
  POST /api/admin/act-as-supplier/{account_id}/reset-password
"""
import os
import pytest
import requests
from pymongo import MongoClient

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://sap-data-sync.preview.emergentagent.com").rstrip("/")
POS_TOKEN = "iter183actassupplierpos000000000000000000000000"
NEG_TOKEN = "iter183actassupplierneg000000000000000000000000"
S9999_ACCOUNT_ID = "3bbb5d32-cd5d-4d31-9fd7-042b6c548174"  # vendor_code S9999 - dummy fixture

MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "test_database")


@pytest.fixture(scope="module")
def db():
    return MongoClient(MONGO_URL)[DB_NAME]


@pytest.fixture
def pos_client():
    s = requests.Session()
    s.cookies.set("vms_session", POS_TOKEN)
    return s


@pytest.fixture
def neg_client():
    s = requests.Session()
    s.cookies.set("vms_session", NEG_TOKEN)
    return s


# ---------- Auth gating ----------
class TestAuthGating:
    def test_accounts_no_cookie_returns_401(self):
        r = requests.get(f"{BASE_URL}/api/admin/act-as-supplier/accounts")
        assert r.status_code == 401, r.text

    def test_pos_no_cookie_returns_401(self):
        r = requests.get(f"{BASE_URL}/api/admin/act-as-supplier/{S9999_ACCOUNT_ID}/purchase-orders")
        assert r.status_code == 401

    def test_shipment_no_cookie_returns_401(self):
        r = requests.post(f"{BASE_URL}/api/admin/act-as-supplier/{S9999_ACCOUNT_ID}/shipments", json={"items": []})
        assert r.status_code == 401

    def test_reset_password_no_cookie_returns_401(self):
        r = requests.post(f"{BASE_URL}/api/admin/act-as-supplier/{S9999_ACCOUNT_ID}/reset-password", json={"new_password": "abcdefgh"})
        assert r.status_code == 401

    def test_accounts_forbidden_without_permission(self, neg_client):
        r = neg_client.get(f"{BASE_URL}/api/admin/act-as-supplier/accounts")
        assert r.status_code == 403, r.text

    def test_purchase_orders_forbidden_without_permission(self, neg_client):
        r = neg_client.get(f"{BASE_URL}/api/admin/act-as-supplier/{S9999_ACCOUNT_ID}/purchase-orders")
        assert r.status_code == 403


# ---------- GET endpoints (positive session) ----------
class TestReadEndpoints:
    def test_list_accounts_returns_only_approved(self, pos_client):
        r = pos_client.get(f"{BASE_URL}/api/admin/act-as-supplier/accounts")
        assert r.status_code == 200, r.text
        data = r.json()
        assert "accounts" in data
        assert isinstance(data["accounts"], list)
        assert len(data["accounts"]) > 0
        # All should be approved
        for a in data["accounts"]:
            assert a.get("status") == "approved", f"non-approved account returned: {a}"
            assert "password_hash" not in a, "password_hash leaked in response"
        # Our target should be there
        ids = [a["_id"] for a in data["accounts"]]
        assert S9999_ACCOUNT_ID in ids

    def test_get_purchase_orders_for_s9999(self, pos_client):
        r = pos_client.get(f"{BASE_URL}/api/admin/act-as-supplier/{S9999_ACCOUNT_ID}/purchase-orders")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["vendor_code"] == "S9999"
        assert "purchase_orders" in data
        # S9999 has 3 fixture PO lines seeded
        pos = data["purchase_orders"]
        assert isinstance(pos, list)
        # remaining_qty must be included
        if pos:
            assert "remaining_qty" in pos[0]

    def test_get_purchase_orders_invalid_account_404(self, pos_client):
        r = pos_client.get(f"{BASE_URL}/api/admin/act-as-supplier/nonexistent-id-xxx/purchase-orders")
        assert r.status_code == 404


# ---------- Reset password ----------
class TestResetPassword:
    def test_reset_password_short_returns_400(self, pos_client):
        r = pos_client.post(
            f"{BASE_URL}/api/admin/act-as-supplier/{S9999_ACCOUNT_ID}/reset-password",
            json={"new_password": "short"},
        )
        assert r.status_code == 400
        assert "8 char" in r.text.lower() or "at least 8" in r.text.lower()

    def test_reset_password_invalid_account_returns_404(self, pos_client):
        r = pos_client.post(
            f"{BASE_URL}/api/admin/act-as-supplier/nonexistent-xxx/reset-password",
            json={"new_password": "ValidPass123"},
        )
        assert r.status_code == 404

    def test_reset_password_updates_hash_and_supplier_can_login(self, pos_client, db):
        """Set a known password via the endpoint. Verify hash was persisted
        in MongoDB, then confirm the supplier can actually log into the
        Supplier Portal with that new password. The endpoint returns 502
        if Graph email send fails (test env has no Azure Mail permission),
        but per implementation the password IS still updated first.
        """
        old_hash = db["supplier_portal_accounts"].find_one({"_id": S9999_ACCOUNT_ID}, {"password_hash": 1})["password_hash"]

        new_pw = "Iter183NewPass!"
        r = pos_client.post(
            f"{BASE_URL}/api/admin/act-as-supplier/{S9999_ACCOUNT_ID}/reset-password",
            json={"new_password": new_pw},
        )
        # Accept both success (200) and 502 (email couldn't send in this env - password still updated per code path)
        assert r.status_code in (200, 502), f"unexpected status: {r.status_code} - {r.text}"

        # Verify hash actually changed in DB
        new_hash = db["supplier_portal_accounts"].find_one({"_id": S9999_ACCOUNT_ID}, {"password_hash": 1})["password_hash"]
        assert new_hash != old_hash, "password_hash was not updated in MongoDB"
        assert new_hash.startswith("$2b$") or new_hash.startswith("$2a$"), f"bad bcrypt format: {new_hash[:10]}"

        # Verify supplier can log in with the new password
        login = requests.post(
            f"{BASE_URL}/api/supplier-portal/login",
            json={"email": "vendor1@testco.com", "password": new_pw},
        )
        assert login.status_code == 200, f"fresh login failed: {login.status_code} - {login.text}"

        # Restore the documented password 'DummyTest123' for continuity with test_credentials.md
        restore = pos_client.post(
            f"{BASE_URL}/api/admin/act-as-supplier/{S9999_ACCOUNT_ID}/reset-password",
            json={"new_password": "DummyTest123"},
        )
        assert restore.status_code in (200, 502)


# ---------- Shipment creation ----------
class TestShipmentCreation:
    def test_create_shipment_stamps_created_on_behalf_by(self, pos_client, db):
        # Pick 1 EA of S9999 fixture PO TESTGRN1/item 1
        r = pos_client.post(
            f"{BASE_URL}/api/admin/act-as-supplier/{S9999_ACCOUNT_ID}/shipments",
            json={"items": [{"po_number": "TESTGRN1", "item_number": "1", "ship_qty": 1}]},
        )
        assert r.status_code == 200, f"shipment create failed: {r.status_code} - {r.text}"
        doc = r.json()
        assert "_id" in doc
        assert doc["created_on_behalf_by"] == "Iter183 Pos Tester"
        assert doc["vendor_code"] == "S9999"
        assert doc["status"] == "in_transit"

        # Verify in MongoDB directly
        db_doc = db["supplier_portal_shipments"].find_one({"_id": doc["_id"]})
        assert db_doc is not None
        assert db_doc["created_on_behalf_by"] == "Iter183 Pos Tester"

        # Cleanup: mark it rejected or delete outright to keep DB clean and open qty free
        db["supplier_portal_shipments"].delete_one({"_id": doc["_id"]})

    def test_create_shipment_empty_items_400(self, pos_client):
        r = pos_client.post(
            f"{BASE_URL}/api/admin/act-as-supplier/{S9999_ACCOUNT_ID}/shipments",
            json={"items": []},
        )
        assert r.status_code == 400
