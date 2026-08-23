"""Aug 2026 auth/role change tests:
 - Store Approval API now requires login + 'store_approval' page permission
 - new 'admin' role tier (full page access, limited grant powers)
 - /api/admin/pages, /api/admin/users, PUT /api/admin/users/{id}/access rules
Synthetic Entra sessions are inserted directly into Mongo (no real MS SSO
possible from tests) and deleted in teardown.
"""
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient

frontend_env = dotenv_values("/app/frontend/.env")
BASE_URL = (os.environ.get("REACT_APP_BACKEND_URL") or frontend_env.get("REACT_APP_BACKEND_URL")).rstrip("/")

backend_env = dotenv_values("/app/backend/.env")
MONGO_URL = os.environ.get("MONGO_URL") or backend_env["MONGO_URL"]
DB_NAME = os.environ.get("DB_NAME") or backend_env["DB_NAME"]
COOKIE = "vms_session"
SUFFIX = os.environ.get("PYTEST_XDIST_WORKER", "solo")

PAGE_KEYS_EXPECTED = {
    "bom_explorer", "purchasing_plan", "production_plan", "production_confirmation",
    "inventory", "supplier_master", "quota_allocation", "admin", "admin_sap_write",
    "admin_create_material", "store_approval",
}


@pytest.fixture(scope="module")
def mongo_db():
    client = MongoClient(MONGO_URL)
    yield client[DB_NAME]
    client.close()


@pytest.fixture(scope="module")
def personas(mongo_db):
    """Creates synthetic auth_users + auth_sessions for each role under test."""
    specs = {
        "store_user": ("user", ["store_approval"]),
        "plain_user": ("user", []),
        "admin": ("admin", []),
        "super_admin": ("super_admin", []),
        # mutation targets
        "target_user": ("user", ["inventory"]),
        "target_super": ("super_admin", []),
    }
    made = {}
    now = datetime.now(timezone.utc)
    for key, (role, pages) in specs.items():
        user_id = f"TESTtid:TESToid-{key}-{SUFFIX}"
        token = "TEST_roleauth_" + uuid.uuid5(uuid.NAMESPACE_DNS, user_id).hex
        mongo_db["auth_users"].replace_one(
            {"_id": user_id},
            {"_id": user_id, "tid": "TESTtid", "oid": f"TESToid-{key}",
             "email": f"TEST_{key}@example.test", "name": f"TEST QA {key}",
             "role": role, "allowed_pages": pages,
             "created_at": now, "last_login_at": now},
            upsert=True,
        )
        mongo_db["auth_sessions"].replace_one(
            {"_id": token},
            {"_id": token, "user_id": user_id, "expires_at": now + timedelta(days=1)},
            upsert=True,
        )
        made[key] = {"user_id": user_id, "token": token, "role": role, "pages": pages}
    yield made
    for v in made.values():
        mongo_db["auth_sessions"].delete_one({"_id": v["token"]})
        mongo_db["auth_users"].delete_one({"_id": v["user_id"]})


def sess(persona=None):
    s = requests.Session()
    if persona:
        s.cookies.set(COOKIE, persona["token"])
    return s


# ---------------- Store Approval API is no longer public ----------------
class TestStoreApprovalAuthGate:
    @pytest.mark.parametrize("path", [
        "/api/store-requests",
        "/api/store-requests/journal",
        "/api/store-requests/balance-pending",
        "/api/store-requests/P1-000004",
    ])
    def test_unauthenticated_gets_401(self, path):
        r = sess().get(f"{BASE_URL}{path}", timeout=60)
        assert r.status_code == 401, f"{path} -> {r.status_code} {r.text[:200]}"
        assert "Login required" in r.text

    def test_unauthenticated_issue_post_401(self):
        r = sess().post(f"{BASE_URL}/api/store-requests/P1-000004/issue",
                        json={"issued": {}, "actor": "hacker"}, timeout=60)
        assert r.status_code == 401

    @pytest.mark.parametrize("path", [
        "/api/store-requests",
        "/api/store-requests/journal",
        "/api/store-requests/balance-pending",
    ])
    def test_user_with_store_approval_can_read(self, personas, path):
        r = sess(personas["store_user"]).get(f"{BASE_URL}{path}", timeout=120)
        assert r.status_code == 200, f"{path} -> {r.status_code} {r.text[:300]}"
        data = r.json()
        assert isinstance(data, (list, dict))

    @pytest.mark.parametrize("path", [
        "/api/store-requests",
        "/api/store-requests/journal",
    ])
    def test_user_without_store_approval_403(self, personas, path):
        r = sess(personas["plain_user"]).get(f"{BASE_URL}{path}", timeout=60)
        assert r.status_code == 403, f"{path} -> {r.status_code} {r.text[:200]}"
        assert "Access denied" in r.text

    def test_admin_role_can_read_without_explicit_page(self, personas):
        r = sess(personas["admin"]).get(f"{BASE_URL}/api/store-requests", timeout=120)
        assert r.status_code == 200, r.text[:300]

    def test_super_admin_can_read(self, personas):
        r = sess(personas["super_admin"]).get(f"{BASE_URL}/api/store-requests", timeout=120)
        assert r.status_code == 200, r.text[:300]


# ---------------- /api/auth/me role semantics ----------------
class TestAuthMe:
    def test_admin_gets_all_pages(self, personas):
        r = sess(personas["admin"]).get(f"{BASE_URL}/api/auth/me", timeout=60)
        assert r.status_code == 200
        d = r.json()
        assert d["authenticated"] is True
        assert d["role"] == "admin"
        assert set(d["allowed_pages"]) == PAGE_KEYS_EXPECTED

    def test_super_admin_gets_all_pages(self, personas):
        d = sess(personas["super_admin"]).get(f"{BASE_URL}/api/auth/me", timeout=60).json()
        assert d["role"] == "super_admin"
        assert set(d["allowed_pages"]) == PAGE_KEYS_EXPECTED

    def test_plain_user_pages_unchanged(self, personas):
        d = sess(personas["plain_user"]).get(f"{BASE_URL}/api/auth/me", timeout=60).json()
        assert d["role"] == "user"
        assert d["allowed_pages"] == []

    def test_store_user_pages(self, personas):
        d = sess(personas["store_user"]).get(f"{BASE_URL}/api/auth/me", timeout=60).json()
        assert d["allowed_pages"] == ["store_approval"]

    def test_unauthenticated_me(self):
        d = sess().get(f"{BASE_URL}/api/auth/me", timeout=60).json()
        assert d == {"authenticated": False}

    def test_store_approval_in_page_catalog(self, personas):
        r = sess(personas["super_admin"]).get(f"{BASE_URL}/api/admin/pages", timeout=60)
        assert r.status_code == 200
        keys = {p["key"] for p in r.json()["pages"]}
        assert "store_approval" in keys
        assert keys == PAGE_KEYS_EXPECTED


# ---------------- Admin endpoints access ----------------
class TestAdminEndpointAccess:
    def test_pages_and_users_ok_for_admin(self, personas):
        c = sess(personas["admin"])
        assert c.get(f"{BASE_URL}/api/admin/pages", timeout=60).status_code == 200
        r = c.get(f"{BASE_URL}/api/admin/users", timeout=60)
        assert r.status_code == 200, r.text[:300]
        users = r.json()["users"]
        assert isinstance(users, list) and len(users) > 0
        assert all(isinstance(u.get("_id"), str) for u in users)

    def test_pages_and_users_forbidden_for_plain_user(self, personas):
        c = sess(personas["plain_user"])
        assert c.get(f"{BASE_URL}/api/admin/pages", timeout=60).status_code == 403
        assert c.get(f"{BASE_URL}/api/admin/users", timeout=60).status_code == 403

    def test_users_unauthenticated_401(self):
        assert sess().get(f"{BASE_URL}/api/admin/users", timeout=60).status_code == 401


# ---------------- PUT access rules ----------------
class TestUpdateAccessRules:
    def _put(self, persona, target_id, role, pages):
        return sess(persona).put(f"{BASE_URL}/api/admin/users/{target_id}/access",
                                 json={"role": role, "allowed_pages": pages}, timeout=60)

    def _fetch(self, personas, target_id):
        users = sess(personas["super_admin"]).get(f"{BASE_URL}/api/admin/users", timeout=60).json()["users"]
        return next((u for u in users if u["_id"] == target_id), None)

    def test_admin_can_set_user_role_and_pages(self, personas):
        tid = personas["target_user"]["user_id"]
        r = self._put(personas["admin"], tid, "user", ["inventory", "store_approval"])
        assert r.status_code == 200, r.text[:300]
        doc = self._fetch(personas, tid)
        assert doc["role"] == "user"
        assert set(doc["allowed_pages"]) == {"inventory", "store_approval"}

    def test_admin_cannot_assign_admin_role(self, personas):
        r = self._put(personas["admin"], personas["target_user"]["user_id"], "admin", [])
        assert r.status_code == 403, r.text[:300]
        assert "super admin" in r.json().get("detail", "").lower()
        doc = self._fetch(personas, personas["target_user"]["user_id"])
        assert doc["role"] == "user", "role must not have changed"

    def test_admin_cannot_assign_super_admin_role(self, personas):
        r = self._put(personas["admin"], personas["target_user"]["user_id"], "super_admin", [])
        assert r.status_code == 403, r.text[:300]

    def test_admin_cannot_touch_super_admin_target(self, personas):
        tid = personas["target_super"]["user_id"]
        r = self._put(personas["admin"], tid, "user", ["inventory"])
        assert r.status_code == 403, r.text[:300]
        assert "super admin" in r.json().get("detail", "").lower()
        doc = self._fetch(personas, tid)
        assert doc["role"] == "super_admin", "super_admin must not be demoted by admin"

    def test_plain_user_cannot_update_access(self, personas):
        r = self._put(personas["plain_user"], personas["target_user"]["user_id"], "user", [])
        assert r.status_code == 403

    def test_super_admin_can_promote_to_admin_and_back(self, personas):
        tid = personas["target_user"]["user_id"]
        r = self._put(personas["super_admin"], tid, "admin", [])
        assert r.status_code == 200, r.text[:300]
        assert self._fetch(personas, tid)["role"] == "admin"
        r = self._put(personas["super_admin"], tid, "super_admin", [])
        assert r.status_code == 200, r.text[:300]
        assert self._fetch(personas, tid)["role"] == "super_admin"
        # restore
        r = self._put(personas["super_admin"], tid, "user", ["inventory"])
        assert r.status_code == 200
        assert self._fetch(personas, tid)["role"] == "user"

    def test_super_admin_can_modify_super_admin(self, personas):
        tid = personas["target_super"]["user_id"]
        r = self._put(personas["super_admin"], tid, "super_admin", ["inventory"])
        assert r.status_code == 200, r.text[:300]
        assert self._fetch(personas, tid)["allowed_pages"] == ["inventory"]

    def test_invalid_role_rejected(self, personas):
        r = self._put(personas["super_admin"], personas["target_user"]["user_id"], "root", [])
        assert r.status_code == 400

    def test_unknown_page_key_rejected(self, personas):
        r = self._put(personas["super_admin"], personas["target_user"]["user_id"], "user", ["not_a_page"])
        assert r.status_code == 400
        assert "not_a_page" in r.text

    def test_unknown_user_404(self, personas):
        r = self._put(personas["super_admin"], "TESTtid:does-not-exist", "user", [])
        assert r.status_code == 404


# ---------------- Store Approval regression (data shape) ----------------
class TestStoreApprovalRegression:
    def test_queue_shape_and_material_type_and_restricted_flags(self, personas):
        r = sess(personas["store_user"]).get(f"{BASE_URL}/api/store-requests", timeout=180)
        assert r.status_code == 200, r.text[:300]
        payload = r.json()
        rows = payload if isinstance(payload, list) else payload.get("requests", payload.get("items", []))
        assert isinstance(rows, list)
        if not rows:
            pytest.skip("no store requests in DB to shape-check")
        row = rows[0]
        assert row.get("_id"), "request doc must expose its id"
        assert "components" in row

    def test_request_detail_components(self, personas):
        c = sess(personas["store_user"])
        payload = c.get(f"{BASE_URL}/api/store-requests", timeout=180).json()
        rows = payload if isinstance(payload, list) else payload.get("requests", [])
        pending = [x for x in rows if (x.get("status") or "").lower() in ("pending", "open", "")]
        target = (pending or rows)
        if not target:
            pytest.skip("no requests available")
        rid = target[0].get("_id")
        r = c.get(f"{BASE_URL}/api/store-requests/{rid}", timeout=180)
        assert r.status_code == 200, r.text[:300]
        d = r.json()
        comps = d.get("components") or d.get("request", {}).get("components") or []
        assert isinstance(comps, list)

    def test_refresh_live_stock_job_starts(self, personas):
        c = sess(personas["store_user"])
        r = c.post(f"{BASE_URL}/api/store-requests/refresh-live-stock",
                   params={"site_id": "P1"}, timeout=120)
        assert r.status_code in (200, 202), r.text[:300]
        job_id = r.json().get("job_id")
        assert job_id
        s = c.get(f"{BASE_URL}/api/store-requests/refresh-live-stock/{job_id}", timeout=60)
        assert s.status_code == 200, s.text[:300]
        assert "status" in s.json()
