"""Iteration 194 tests: SAP webhook receiver, GRN Check Now endpoint, and regression."""
import os
import base64
import time
import pytest
import requests
from pymongo import MongoClient

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://sap-data-sync.preview.emergentagent.com").rstrip("/")
SESSION_COOKIE = "iter183actassupplierpos000000000000000000000000"
WH_USER = "sap_bydesign"
WH_PASS = "v-TYA5VXvE8mgdjPoH4StRYOILJBmbWh"


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    s.cookies.set("vms_session", SESSION_COOKIE)
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module")
def mongo_db():
    from dotenv import load_dotenv
    load_dotenv("/app/backend/.env")
    mongo_url = os.environ.get("MONGO_URL")
    db_name = os.environ.get("DB_NAME")
    client = MongoClient(mongo_url)
    return client[db_name]


# ---------- Backend health ----------
class TestHealth:
    def test_root_fast(self):
        t0 = time.time()
        r = requests.get(f"{BASE_URL}/api/")
        elapsed = time.time() - t0
        assert r.status_code == 200
        assert r.json().get("message")
        assert elapsed < 2, f"Root took {elapsed:.2f}s"

    def test_act_as_supplier_accounts_fast(self, admin_session):
        t0 = time.time()
        r = admin_session.get(f"{BASE_URL}/api/admin/act-as-supplier/accounts", timeout=10)
        elapsed = time.time() - t0
        assert r.status_code == 200, f"got {r.status_code}: {r.text[:200]}"
        assert elapsed < 2, f"accounts endpoint took {elapsed:.2f}s"


# ---------- Webhook receiver ----------
class TestSapWebhook:
    URL = f"{BASE_URL}/api/webhooks/sap-put-away"
    PAYLOAD = {"test": True, "iter": 194, "source": "testing_agent"}

    def test_missing_auth_returns_401(self):
        r = requests.post(self.URL, json=self.PAYLOAD)
        assert r.status_code == 401, f"got {r.status_code}: {r.text[:200]}"

    def test_wrong_auth_returns_401(self):
        r = requests.post(self.URL, json=self.PAYLOAD, auth=("wrong", "wrong"))
        assert r.status_code == 401

    def test_correct_auth_returns_200_and_persists(self, mongo_db):
        marker = f"iter194-marker-{int(time.time())}"
        payload = dict(self.PAYLOAD, marker=marker)
        r = requests.post(self.URL, json=payload, auth=(WH_USER, WH_PASS))
        assert r.status_code == 200, f"got {r.status_code}: {r.text[:200]}"
        data = r.json()
        assert data.get("status") == "received"
        # verify persistence
        doc = mongo_db["sap_webhook_events"].find_one({"parsed_body.marker": marker})
        assert doc is not None, "Webhook payload not persisted in sap_webhook_events"
        assert doc["parsed_body"]["marker"] == marker
        # cleanup
        mongo_db["sap_webhook_events"].delete_one({"_id": doc["_id"]})

    def test_no_session_cookie_required(self):
        """Route must NOT require vms_session cookie - SAP calls with none."""
        s = requests.Session()  # no cookie
        r = s.post(self.URL, json={"foo": "bar"}, auth=(WH_USER, WH_PASS))
        assert r.status_code == 200


# ---------- GRN Check Now ----------
class TestGrnCheckNow:
    @pytest.fixture(scope="class")
    def posted_shipment(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/admin/grn/shipments?status=approved", timeout=30)
        assert r.status_code == 200, f"confirmed list failed: {r.status_code}"
        items = r.json().get("shipments", [])
        # find a posted shipment
        posted = [x for x in items if x.get("sap_sync_status") == "posted"]
        if not posted:
            pytest.skip("No posted GRN shipments available to test check-now")
        # prefer S000040 if present
        for x in posted:
            if x.get("_id") == "S000040":
                return x
        return posted[0]

    def test_check_now_returns_doc(self, admin_session, posted_shipment):
        doc_code = posted_shipment["_id"]
        r = admin_session.post(f"{BASE_URL}/api/admin/grn/{doc_code}/check-now", timeout=120)
        assert r.status_code == 200, f"got {r.status_code}: {r.text[:300]}"
        data = r.json()
        assert data.get("_id") == doc_code
        assert "sap_sync_status" in data

    def test_check_now_idempotent(self, admin_session, posted_shipment):
        doc_code = posted_shipment["_id"]
        r1 = admin_session.post(f"{BASE_URL}/api/admin/grn/{doc_code}/check-now", timeout=120)
        r2 = admin_session.post(f"{BASE_URL}/api/admin/grn/{doc_code}/check-now", timeout=120)
        assert r1.status_code == 200
        assert r2.status_code == 200
        # should still be posted, no crash
        assert r2.json().get("sap_sync_status") == "posted"


# ---------- GRN regression ----------
class TestGrnRegression:
    def test_pending_endpoint(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/admin/grn/shipments?status=pending", timeout=30)
        assert r.status_code == 200
        assert isinstance(r.json().get("shipments"), list)

    def test_confirmed_endpoint(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/admin/grn/shipments?status=approved", timeout=30)
        assert r.status_code == 200
        assert isinstance(r.json().get("shipments"), list)

    def test_retry_put_away_on_non_applicable(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/admin/grn/shipments?status=approved", timeout=30)
        items = r.json().get("shipments", [])
        # pick one where sap_sync_status != put_away_failed
        target = None
        for x in items:
            if x.get("sap_sync_status") != "put_away_failed":
                target = x
                break
        if not target:
            pytest.skip("No non-put-away-failed shipment for negative retry test")
        doc_code = target["_id"]
        r = admin_session.post(f"{BASE_URL}/api/admin/grn/{doc_code}/retry-put-away", timeout=60)
        # Should either return 400 (not applicable) or 200 (no-op), but never 500
        assert r.status_code < 500, f"Server error on retry-put-away: {r.status_code} {r.text[:200]}"


# ---------- STO regression ----------
class TestStoRegression:
    def test_sto_list_loads(self, admin_session):
        # Try common STO endpoints
        for path in ["/api/admin/sto", "/api/sto", "/api/admin/stock-transfer"]:
            r = admin_session.get(f"{BASE_URL}{path}", timeout=30)
            if r.status_code == 200:
                return
        pytest.skip("No STO list endpoint found at common paths")
