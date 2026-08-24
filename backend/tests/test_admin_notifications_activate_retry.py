"""Session 20 feature: admin "Action Needed" notification panel +
POST /api/admin/material-sites/activate + POST /api/stock-transfer/orders/{sto_id}/retry.

Roles are simulated with synthetic auth_users/auth_sessions docs (same
technique documented in /app/memory/test_credentials.md). Run
`python _setup_role_sessions.py` first (or rely on the module fixture below).
"""
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient

frontend_env = dotenv_values("/app/frontend/.env")
BASE_URL = (os.environ.get("REACT_APP_BACKEND_URL") or frontend_env.get("REACT_APP_BACKEND_URL")).rstrip("/")

backend_env = dotenv_values("/app/backend/.env")
COOKIE_NAME = "vms_session"

SAFE_PRODUCT_ID = "P27175"   # per main agent: fully configured everywhere, safe idempotent SAP write
SAFE_SITE_ID = "P1"


@pytest.fixture(scope="module")
def mongo_db():
    client = MongoClient(backend_env["MONGO_URL"])
    yield client[backend_env["DB_NAME"]]
    client.close()


@pytest.fixture(scope="module")
def sessions(mongo_db):
    """Creates 3 synthetic sessions: plain user (with inventory page access),
    admin, super_admin. Cleaned up at module teardown."""
    now = datetime.now(timezone.utc)
    specs = {
        "user": ("user", ["inventory"]),
        "admin": ("admin", []),
        "super_admin": ("super_admin", []),
    }
    worker = os.environ.get("PYTEST_XDIST_WORKER", "gw")
    tokens = {}
    for key, (role, pages) in specs.items():
        user_id = f"TESTtid:TESToid-notif-{worker}-{key}"
        token = "TEST_notif_" + uuid.uuid5(uuid.NAMESPACE_DNS, user_id).hex
        mongo_db["auth_users"].replace_one(
            {"_id": user_id},
            {"_id": user_id, "tid": "TESTtid", "oid": f"TESToid-notif-{key}",
             "email": f"TEST_notif_{worker}_{key}@example.test", "name": f"TEST QA {key}",
             "role": role, "allowed_pages": pages,
             "created_at": now, "last_login_at": now},
            upsert=True,
        )
        mongo_db["auth_sessions"].replace_one(
            {"_id": token},
            {"_id": token, "user_id": user_id, "expires_at": now + timedelta(days=1)},
            upsert=True,
        )
        tokens[key] = token
    yield tokens
    for key in specs:
        user_id = f"TESTtid:TESToid-notif-{worker}-{key}"
        mongo_db["auth_users"].delete_one({"_id": user_id})
        mongo_db["auth_sessions"].delete_one({"_id": tokens[key]})


def client_for(token):
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json", "Cookie": f"{COOKIE_NAME}={token}"})
    return s


# --- GET /api/admin/notifications (auth gating) ---
class TestAdminNotificationsAuth:
    def test_no_session_returns_401(self):
        r = requests.get(f"{BASE_URL}/api/admin/notifications")
        assert r.status_code == 401, r.text

    def test_role_user_forbidden(self, sessions):
        r = client_for(sessions["user"]).get(f"{BASE_URL}/api/admin/notifications")
        assert r.status_code == 403, f"expected 403 for role=user, got {r.status_code}: {r.text[:300]}"

    @pytest.mark.parametrize("role", ["admin", "super_admin"])
    def test_admin_roles_allowed(self, sessions, role):
        r = client_for(sessions[role]).get(f"{BASE_URL}/api/admin/notifications")
        assert r.status_code == 200, r.text
        data = r.json()
        assert isinstance(data.get("notifications"), list)
        for n in data["notifications"]:
            assert "product_id" in n and "site_id" in n and "message" in n
            assert isinstance(n["_id"], str)


# --- notification creation + resolve (service-level, no SAP write) ---
class TestNotificationLifecycle:
    def test_regex_match_creates_and_dedupes(self, mongo_db, sessions):
        import sys
        sys.path.insert(0, "/app/backend")
        import stock_transfer_service as svc

        msg = "No valid planning data exists for product TESTPROD1 in site TESTSITE1"
        svc._create_missing_planning_notification_if_matched(mongo_db, "STO-TEST-1", msg)
        svc._create_missing_planning_notification_if_matched(mongo_db, "STO-TEST-2", msg)
        docs = list(mongo_db[svc.NOTIFICATIONS_COLLECTION].find(
            {"product_id": "TESTPROD1", "site_id": "TESTSITE1"}))
        assert len(docs) == 1, f"expected dedupe/upsert, got {len(docs)} docs"
        assert docs[0]["sto_id"] == "STO-TEST-2"
        nid = docs[0]["_id"]

        # visible through the admin API
        r = client_for(sessions["admin"]).get(f"{BASE_URL}/api/admin/notifications")
        assert r.status_code == 200
        assert any(n["_id"] == nid for n in r.json()["notifications"])

        # non-matching errors must NOT create notifications
        svc._create_missing_planning_notification_if_matched(mongo_db, "STO-TEST-3", "Some other SAP error")
        assert mongo_db[svc.NOTIFICATIONS_COLLECTION].count_documents({"sto_id": "STO-TEST-3"}) == 0

        # resolve hides it
        svc.resolve_admin_notification(mongo_db, nid)
        r2 = client_for(sessions["admin"]).get(f"{BASE_URL}/api/admin/notifications")
        assert not any(n["_id"] == nid for n in r2.json()["notifications"])

        mongo_db[svc.NOTIFICATIONS_COLLECTION].delete_one({"_id": nid})


# --- POST /api/admin/material-sites/activate ---
class TestActivateMaterialSite:
    def test_no_session_401(self):
        r = requests.post(f"{BASE_URL}/api/admin/material-sites/activate",
                          json={"product_id": SAFE_PRODUCT_ID, "site_id": SAFE_SITE_ID})
        assert r.status_code == 401, r.text

    def test_role_user_forbidden(self, sessions):
        r = client_for(sessions["user"]).post(
            f"{BASE_URL}/api/admin/material-sites/activate",
            json={"product_id": SAFE_PRODUCT_ID, "site_id": SAFE_SITE_ID})
        assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text[:300]}"

    def test_missing_fields_422(self, sessions):
        r = client_for(sessions["admin"]).post(
            f"{BASE_URL}/api/admin/material-sites/activate", json={"product_id": SAFE_PRODUCT_ID})
        assert r.status_code == 422, r.text

    def test_admin_activate_safe_product_site(self, sessions):
        """REAL live SAP write - only the main-agent-approved safe pair."""
        r = client_for(sessions["admin"]).post(
            f"{BASE_URL}/api/admin/material-sites/activate",
            json={"product_id": SAFE_PRODUCT_ID, "site_id": SAFE_SITE_ID}, timeout=180)
        assert r.status_code == 200, f"got {r.status_code}: {r.text[:500]}"
        data = r.json()
        assert "planning_logistics" in data and "valuation" in data
        assert isinstance(data["planning_logistics"], str)
        assert isinstance(data["valuation"], str)
        print(f"ACTIVATE RESULT: {data}")
        # Idempotency expectation: P27175/P1 is already fully set up, so a
        # re-activation should be reported as a success to the admin, not as
        # a red failure. SAP returns "Similar entry already exists" here and
        # the client maps that to an error string -> blocks the Retry step.
        assert data["planning_logistics"] == "ok", (
            "Re-activating an already-active site is reported as a failure: "
            f"{data['planning_logistics']!r}")


# --- POST /api/stock-transfer/orders/{sto_id}/retry ---
class TestRetryOrder:
    def test_no_session_401(self):
        r = requests.post(f"{BASE_URL}/api/stock-transfer/orders/STO-000001/retry")
        assert r.status_code == 401

    def test_retry_nonexistent_sto_fails_gracefully(self, sessions):
        c = client_for(sessions["admin"])
        r = c.post(f"{BASE_URL}/api/stock-transfer/orders/STO-999999/retry", timeout=60)
        assert r.status_code != 500, f"server error on bogus sto_id: {r.text[:300]}"
        if r.status_code == 200:
            job_id = r.json().get("sap_job_id")
            assert job_id
            # background job must surface the failure, not hang forever
            status = None
            for _ in range(15):
                time.sleep(2)
                jr = c.get(f"{BASE_URL}/api/stock-transfer/orders/sap-status/{job_id}")
                assert jr.status_code == 200, jr.text
                status = jr.json()
                if status.get("status") in ("failed", "done"):
                    break
            assert status is not None
            assert status.get("status") == "failed", f"expected job failure for bogus STO, got {status}"
            assert "not found" in (status.get("error") or "").lower(), status
            pytest.fail(
                "API accepted retry for a non-existent sto_id with HTTP 200 (job later "
                f"failed with: {status.get('error')}). Expected a 404 at request time.")
        else:
            assert r.status_code == 404, f"expected 404, got {r.status_code}: {r.text[:300]}"

    def test_orders_list_shape(self, sessions):
        r = client_for(sessions["user"]).get(f"{BASE_URL}/api/stock-transfer/orders", timeout=90)
        assert r.status_code == 200, r.text
        orders = r.json()
        assert isinstance(orders, list) and orders, "no STO orders returned"
        o = orders[0]
        for k in ("sto_id", "status", "ship_from_site_id", "ship_to_site_id", "items"):
            assert k in o, f"missing key {k} in {list(o.keys())}"
        assert "_id" not in o, "raw Mongo _id leaked into response"

    def test_gst_note_pushed_flag_present(self, sessions):
        r = client_for(sessions["admin"]).get(f"{BASE_URL}/api/stock-transfer/orders", timeout=90)
        assert r.status_code == 200
        orders = r.json()
        recent = [o for o in orders if o["sto_id"] in ("STO-000023", "STO-000024")]
        assert recent, "STO-000023/24 not found in orders list"
        for o in recent:
            # Informational: STO-000023 predates the note feature and has no
            # gst_note_pushed field at all -> UI renders "—" (not a false
            # "GST Push Failed"), which is acceptable. Only assert the newest.
            print(f"{o['sto_id']}: status={o['status']} gst_note_pushed={o.get('gst_note_pushed')}")
        newest = [o for o in recent if o["sto_id"] == "STO-000024"][0]
        assert newest.get("gst_note_pushed") is True, f"STO-000024 gst_note_pushed={newest.get('gst_note_pushed')}"
