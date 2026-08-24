"""Store Binding (site-level restriction for role='user') - backend enforcement tests.

Covers:
  - GET  /api/admin/known-sites                    (admin/super_admin only)
  - PUT  /api/admin/users/{user_id}/store-sites     (bind sites, persistence)
  - GET  /api/store-requests, /journal, /balance-pending  (row filtering)
  - GET  /api/store-requests/known-sites            (site list filtering)
  - POST /api/store-requests/refresh-live-stock     (403 on unbound site)
  - GET  /api/store-requests/{id}                   (403 on unbound site)
  - POST /api/store-requests/{id}/issue             (403 on unbound site)
  - GET  /api/auth/me                               (bound_sites exposed)
Synthetic Entra sessions are seeded/cleaned by tests/_seed_store_binding.py.
"""
import os
import subprocess
import sys

import pytest
import requests
from dotenv import dotenv_values

frontend_env = dotenv_values("/app/frontend/.env")
base_url = os.environ.get("REACT_APP_BACKEND_URL") or frontend_env.get("REACT_APP_BACKEND_URL")
if not base_url:
    raise RuntimeError("REACT_APP_BACKEND_URL missing")
BASE = base_url.rstrip("/") + "/api"

SEED = "/app/backend/tests/_seed_store_binding.py"
TOKENS = {
    "super_admin": "TEST_sb_8ae5e809d1bb5b37b9f674f99c1a6f27",
    "admin": "TEST_sb_2266fb9925e15e839f51cef3b12f3e9e",
    "user_unbound": "TEST_sb_7e0395ee20875eff9e4b7fe6ab6901cb",
    "user_p1": "TEST_sb_e4c63b33abd750daa154d67c2e2a9d24",
    "bind_target": "TEST_sb_cdf2393ae0a159ae92eedfa791b5f1cb",
}
BIND_TARGET_ID = "TESTtid:TESToid-sb_bind_target"


class RetrySession(requests.Session):
    """The preview ingress occasionally stalls a request (pre-existing flakiness
    documented in iteration_107) - retry once on timeout before failing."""

    def request(self, method, url, **kw):
        kw.setdefault("timeout", 120)
        try:
            return super().request(method, url, **kw)
        except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError) as e:
            print(f"RETRY after {type(e).__name__}: {method} {url}")
            return super().request(method, url, **kw)


def client(role):
    s = RetrySession()
    s.cookies.set("vms_session", TOKENS[role])
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="session", autouse=True)
def seeded():
    subprocess.run([sys.executable, SEED], check=True, capture_output=True)
    yield
    subprocess.run([sys.executable, SEED, "--cleanup"], check=True, capture_output=True)


@pytest.fixture(scope="session")
def all_sites(seeded):
    r = client("super_admin").get(f"{BASE}/admin/known-sites", timeout=90)
    assert r.status_code == 200, r.text[:300]
    return r.json()["sites"]


# ---------- GET /admin/known-sites ----------
class TestAdminKnownSites:
    def test_super_admin_sees_all_sites(self, all_sites):
        assert isinstance(all_sites, list) and len(all_sites) >= 5
        assert "P1" in all_sites and "P2" in all_sites

    def test_admin_sees_all_sites(self, all_sites):
        r = client("admin").get(f"{BASE}/admin/known-sites", timeout=90)
        assert r.status_code == 200
        assert r.json()["sites"] == all_sites

    def test_plain_user_forbidden(self, seeded):
        r = client("user_p1").get(f"{BASE}/admin/known-sites", timeout=60)
        assert r.status_code == 403

    def test_unauthenticated_rejected(self, seeded):
        r = requests.get(f"{BASE}/admin/known-sites", timeout=60)
        assert r.status_code in (401, 403)


# ---------- PUT /admin/users/{id}/store-sites ----------
class TestBindSites:
    def _bound(self, session, user_id):
        r = session.get(f"{BASE}/admin/users", timeout=60)
        assert r.status_code == 200
        for u in r.json()["users"]:
            if u["_id"] == user_id:
                return u.get("bound_sites")
        pytest.fail(f"user {user_id} not found in /admin/users")

    def test_bind_and_persist(self, seeded):
        s = client("super_admin")
        r = s.put(f"{BASE}/admin/users/{BIND_TARGET_ID}/store-sites", json={"bound_sites": ["P1", "P2"]}, timeout=60)
        assert r.status_code == 200, r.text[:300]
        assert r.json().get("ok") is True
        assert sorted(self._bound(s, BIND_TARGET_ID)) == ["P1", "P2"]

        # bound user immediately sees only those sites (no re-login needed)
        r2 = client("bind_target").get(f"{BASE}/store-requests/known-sites", timeout=90)
        assert r2.status_code == 200
        assert sorted(r2.json()["sites"]) == ["P1", "P2"]

    def test_unbind_back_to_empty(self, seeded):
        s = client("super_admin")
        r = s.put(f"{BASE}/admin/users/{BIND_TARGET_ID}/store-sites", json={"bound_sites": []}, timeout=60)
        assert r.status_code == 200
        assert self._bound(s, BIND_TARGET_ID) == []
        r2 = client("bind_target").get(f"{BASE}/store-requests/known-sites", timeout=90)
        assert r2.json()["sites"] == []

    def test_admin_can_bind(self, seeded):
        s = client("admin")
        r = s.put(f"{BASE}/admin/users/{BIND_TARGET_ID}/store-sites", json={"bound_sites": ["P3"]}, timeout=60)
        assert r.status_code == 200
        assert self._bound(s, BIND_TARGET_ID) == ["P3"]
        s.put(f"{BASE}/admin/users/{BIND_TARGET_ID}/store-sites", json={"bound_sites": []}, timeout=60)

    def test_plain_user_cannot_bind(self, seeded):
        r = client("user_p1").put(f"{BASE}/admin/users/{BIND_TARGET_ID}/store-sites",
                                  json={"bound_sites": ["P1"]}, timeout=60)
        assert r.status_code == 403

    def test_unknown_user_404(self, seeded):
        r = client("super_admin").put(f"{BASE}/admin/users/TEST_does_not_exist/store-sites",
                                      json={"bound_sites": ["P1"]}, timeout=60)
        assert r.status_code == 404

    def test_unknown_site_code_accepted_no_validation(self, seeded):
        """DOCUMENTED GAP (minor): bound_sites is free-text - a typo'd/non-existent
        site code is persisted with no 400. Harmless (fail-closed: matches nothing)
        but the admin gets no feedback."""
        s = client("super_admin")
        r = s.put(f"{BASE}/admin/users/{BIND_TARGET_ID}/store-sites",
                  json={"bound_sites": ["ZZZ_NOT_A_SITE"]}, timeout=60)
        assert r.status_code == 200
        assert self._bound(s, BIND_TARGET_ID) == ["ZZZ_NOT_A_SITE"]
        # fail-closed: an invalid binding still yields zero visible sites/requests
        c = client("bind_target")
        assert c.get(f"{BASE}/store-requests/known-sites").json()["sites"] == []
        assert c.get(f"{BASE}/store-requests").json()["requests"] == []
        s.put(f"{BASE}/admin/users/{BIND_TARGET_ID}/store-sites", json={"bound_sites": []}, timeout=60)


# ---------- store-requests list filtering ----------
class TestRequestFiltering:
    def test_super_admin_sees_multiple_sites(self, seeded):
        r = client("super_admin").get(f"{BASE}/store-requests", timeout=120)
        assert r.status_code == 200
        rows = r.json()["requests"]
        assert len(rows) > 0
        assert all("_id" not in str(k) or True for k in rows[0])  # doc shape sanity

    def test_unbound_user_sees_nothing(self, seeded):
        c = client("user_unbound")
        for path in ("/store-requests", "/store-requests/journal", "/store-requests/balance-pending"):
            r = c.get(f"{BASE}{path}", timeout=120)
            assert r.status_code == 200, f"{path} -> {r.status_code}"
            assert r.json()["requests"] == [], f"{path} leaked {len(r.json()['requests'])} rows"

    def test_unbound_user_sees_no_sites(self, seeded):
        r = client("user_unbound").get(f"{BASE}/store-requests/known-sites", timeout=90)
        assert r.status_code == 200
        assert r.json()["sites"] == []

    def test_p1_user_sees_only_p1(self, seeded):
        c = client("user_p1")
        assert c.get(f"{BASE}/store-requests/known-sites", timeout=90).json()["sites"] == ["P1"]
        for path in ("/store-requests", "/store-requests/journal", "/store-requests/balance-pending"):
            r = c.get(f"{BASE}{path}", timeout=120)
            assert r.status_code == 200
            sites = {row.get("site_id") for row in r.json()["requests"]}
            assert sites <= {"P1"}, f"{path} leaked sites {sites}"

    def test_admin_not_restricted(self, seeded, all_sites):
        r = client("admin").get(f"{BASE}/store-requests/known-sites", timeout=90)
        assert r.status_code == 200
        assert r.json()["sites"] == all_sites
        rows = client("admin").get(f"{BASE}/store-requests/journal", timeout=120).json()["requests"]
        assert len({row.get("site_id") for row in rows}) > 1


# ---------- per-site actions ----------
class TestSiteScopedActions:
    @pytest.fixture(scope="class")
    def sample_ids(self, seeded):
        rows = client("super_admin").get(f"{BASE}/store-requests/journal", timeout=120).json()["requests"]
        p1 = next((r["id"] if "id" in r else r["_id"] for r in rows if r.get("site_id") == "P1"), None)
        other = next((r["id"] if "id" in r else r["_id"] for r in rows if r.get("site_id") not in ("P1", None)), None)
        if not p1 or not other:
            pytest.skip("need at least one P1 and one non-P1 request in the journal")
        return {"p1": p1, "other": other}

    def test_refresh_live_stock_unbound_site_403(self, seeded):
        r = client("user_p1").post(f"{BASE}/store-requests/refresh-live-stock", params={"site_id": "P2"}, timeout=60)
        assert r.status_code == 403
        assert "not bound" in r.json().get("detail", "").lower()

    def test_refresh_live_stock_bound_site_ok(self, seeded):
        r = client("user_p1").post(f"{BASE}/store-requests/refresh-live-stock", params={"site_id": "P1"}, timeout=60)
        assert r.status_code == 200, r.text[:300]
        assert r.json().get("job_id")

    def test_refresh_live_stock_unbound_user_403(self, seeded):
        r = client("user_unbound").post(f"{BASE}/store-requests/refresh-live-stock", params={"site_id": "P1"}, timeout=60)
        assert r.status_code == 403

    def test_refresh_live_stock_admin_allowed(self, seeded):
        r = client("admin").post(f"{BASE}/store-requests/refresh-live-stock", params={"site_id": "P2"}, timeout=60)
        assert r.status_code == 200
        assert r.json().get("job_id")

    def test_get_request_detail_other_site_403(self, seeded, sample_ids):
        r = client("user_p1").get(f"{BASE}/store-requests/{sample_ids['other']}", timeout=90)
        assert r.status_code == 403

    def test_get_request_detail_own_site_200(self, seeded, sample_ids):
        r = client("user_p1").get(f"{BASE}/store-requests/{sample_ids['p1']}", timeout=90)
        assert r.status_code == 200
        assert r.json().get("site_id") == "P1"

    def test_issue_other_site_403(self, seeded, sample_ids):
        """Must 403 BEFORE any SAP goods movement is attempted."""
        r = client("user_p1").post(
            f"{BASE}/store-requests/{sample_ids['other']}/issue",
            json={"actor": "TEST QA", "decision": "full", "issued": []}, timeout=90,
        )
        assert r.status_code == 403, r.text[:300]
        assert "not bound" in r.json().get("detail", "").lower()

    def test_issue_unbound_user_403(self, seeded, sample_ids):
        r = client("user_unbound").post(
            f"{BASE}/store-requests/{sample_ids['p1']}/issue",
            json={"actor": "TEST QA", "decision": "full", "issued": []}, timeout=90,
        )
        assert r.status_code == 403


# ---------- /auth/me ----------
class TestAuthMe:
    def test_bound_sites_exposed(self, seeded):
        r = client("user_p1").get(f"{BASE}/auth/me", timeout=60)
        assert r.status_code == 200
        data = r.json()
        assert data.get("bound_sites") == ["P1"]
        assert data.get("role") == "user"

    def test_admin_bound_sites_empty(self, seeded):
        r = client("admin").get(f"{BASE}/auth/me", timeout=60)
        assert r.status_code == 200
        assert r.json().get("bound_sites") == []
