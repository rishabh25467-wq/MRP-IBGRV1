"""Sep 17 2026 - Manual GRN (No-Playwright) feature backend tests.
Tests toggle preference, blocked-users listing, override-block audit,
approve block-check, and non-admin gating. Uses synthetic Mongo sessions
per /app/memory/test_credentials.md."""
import os
import secrets
from datetime import datetime, timezone, timedelta

import pytest
import requests
from pymongo import MongoClient

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "test_database")

USERS = "auth_users"
SESSIONS = "auth_sessions"

_mongo = MongoClient(MONGO_URL)
_db = _mongo[DB_NAME]


def _mk_session(role: str, tag: str, blocked_shipment: str | None = None) -> tuple[str, str]:
    """Insert synthetic auth_users + auth_sessions doc; return (user_id, session_token)."""
    tid = "test-tid-iter178"
    oid = f"{tag}-{secrets.token_hex(4)}"
    user_id = f"{tid}:{oid}"
    email = f"iter178.{tag}@rampgroup.co.in"
    now = datetime.now(timezone.utc)
    user_doc = {
        "_id": user_id, "tid": tid, "oid": oid, "email": email,
        "name": f"Iter178 {tag}", "role": role,
        "allowed_pages": ["vendor_goods_receipt"],
        "bound_sites": ["P2"],
        "created_at": now, "last_login_at": now,
    }
    if blocked_shipment:
        user_doc["grn_blocked_shipment"] = blocked_shipment
        user_doc["grn_blocked_reason"] = f"Auto-test mismatch on {blocked_shipment}"
        user_doc["grn_blocked_at"] = now
    _db[USERS].replace_one({"_id": user_id}, user_doc, upsert=True)
    token = secrets.token_urlsafe(32)
    _db[SESSIONS].insert_one({"_id": token, "user_id": user_id, "expires_at": now + timedelta(days=1)})
    return user_id, token


class _RetrySession(requests.Session):
    """Retry on 502/503/504 - the Cloudflare preview edge is flaky."""
    def request(self, method, url, **kw):
        import time
        last = None
        for i in range(4):
            r = super().request(method, url, timeout=kw.pop("timeout", 30), **kw)
            if r.status_code not in (502, 503, 504):
                return r
            last = r
            time.sleep(2 * (i + 1))
        return last


def _client(token: str) -> requests.Session:
    s = _RetrySession()
    s.cookies.set("vms_session", token)
    s.headers.update({"Content-Type": "application/json"})
    return s


# ---------------- fixtures ----------------
@pytest.fixture(scope="module")
def admin():
    uid, tok = _mk_session("admin", "admin")
    yield {"uid": uid, "token": tok, "client": _client(tok)}
    _db[USERS].delete_one({"_id": uid})
    _db[SESSIONS].delete_one({"_id": tok})


@pytest.fixture(scope="module")
def staff():
    """Regular role='user' GRN staff with no block."""
    uid, tok = _mk_session("user", "staff")
    yield {"uid": uid, "token": tok, "client": _client(tok)}
    _db[USERS].delete_one({"_id": uid})
    _db[SESSIONS].delete_one({"_id": tok})


@pytest.fixture()
def blocked_staff():
    uid, tok = _mk_session("user", "blocked", blocked_shipment="TEST-BLK-Q7YA7Z")
    yield {"uid": uid, "token": tok, "client": _client(tok)}
    _db[USERS].delete_one({"_id": uid})
    _db[SESSIONS].delete_one({"_id": tok})
    _db["grn_mismatch_overrides"].delete_many({"user_id": uid})


# ---------------- Auth /me surface new fields ----------------
class TestAuthMeFields:
    def test_me_exposes_manual_grn_fields(self, staff):
        r = staff["client"].get(f"{BASE_URL}/api/auth/me")
        assert r.status_code == 200, r.text
        data = r.json()
        # `user` key wraps the public view per auth_service.user_public_view
        u = data.get("user", data)
        assert "manual_grn_preference" in u
        assert u["manual_grn_preference"] is False
        assert "grn_blocked_shipment" in u
        assert "grn_blocked_reason" in u

    def test_me_reflects_blocked_state(self, blocked_staff):
        r = blocked_staff["client"].get(f"{BASE_URL}/api/auth/me")
        assert r.status_code == 200
        u = r.json().get("user", r.json())
        assert u["grn_blocked_shipment"] == "TEST-BLK-Q7YA7Z"
        assert "mismatch" in (u.get("grn_blocked_reason") or "").lower()


# ---------------- PUT /admin/grn/my-preference ----------------
class TestManualPreferenceToggle:
    def test_toggle_on_and_off_persists(self, staff):
        r = staff["client"].put(f"{BASE_URL}/api/admin/grn/my-preference", json={"manual_grn_preference": True})
        assert r.status_code == 200, r.text
        assert r.json()["manual_grn_preference"] is True

        me = staff["client"].get(f"{BASE_URL}/api/auth/me").json()
        u = me.get("user", me)
        assert u["manual_grn_preference"] is True

        r2 = staff["client"].put(f"{BASE_URL}/api/admin/grn/my-preference", json={"manual_grn_preference": False})
        assert r2.status_code == 200
        assert r2.json()["manual_grn_preference"] is False
        u2 = staff["client"].get(f"{BASE_URL}/api/auth/me").json()
        u2 = u2.get("user", u2)
        assert u2["manual_grn_preference"] is False

    def test_toggle_requires_auth(self):
        r = requests.put(f"{BASE_URL}/api/admin/grn/my-preference", json={"manual_grn_preference": True})
        assert r.status_code in (401, 403)


# ---------------- GET /admin/grn/blocked-users ----------------
class TestBlockedUsersListing:
    def test_admin_can_list(self, admin, blocked_staff):
        r = admin["client"].get(f"{BASE_URL}/api/admin/grn/blocked-users")
        assert r.status_code == 200, r.text
        users = r.json().get("users", [])
        found = [u for u in users if u["_id"] == blocked_staff["uid"]]
        assert found, "Blocked user should appear in listing"
        assert found[0]["grn_blocked_shipment"] == "TEST-BLK-Q7YA7Z"

    def test_non_admin_forbidden(self, staff):
        r = staff["client"].get(f"{BASE_URL}/api/admin/grn/blocked-users")
        assert r.status_code == 403


# ---------------- POST /admin/grn/override-block/{user_id} ----------------
class TestOverrideBlock:
    def test_empty_reason_rejected(self, admin, blocked_staff):
        r = admin["client"].post(
            f"{BASE_URL}/api/admin/grn/override-block/{blocked_staff['uid']}",
            json={"reason": "   "},
        )
        assert r.status_code == 400
        assert "reason" in r.text.lower()

    def test_non_admin_forbidden(self, staff, blocked_staff):
        r = staff["client"].post(
            f"{BASE_URL}/api/admin/grn/override-block/{blocked_staff['uid']}",
            json={"reason": "Trying to override"},
        )
        assert r.status_code == 403

    def test_valid_override_clears_block_and_logs_audit(self, admin, blocked_staff):
        r = admin["client"].post(
            f"{BASE_URL}/api/admin/grn/override-block/{blocked_staff['uid']}",
            json={"reason": "Iter178 - manually verified SAP posting"},
        )
        assert r.status_code == 200, r.text
        assert r.json().get("ok") is True

        u = _db[USERS].find_one({"_id": blocked_staff["uid"]})
        assert u.get("grn_blocked_shipment") in (None, "")
        assert u.get("grn_blocked_reason") in (None, "")

        audit = list(_db["grn_mismatch_overrides"].find({"user_id": blocked_staff["uid"]}))
        assert len(audit) == 1
        a = audit[0]
        assert a["reason"] == "Iter178 - manually verified SAP posting"
        assert a["doc_code"] == "TEST-BLK-Q7YA7Z"
        assert a.get("overridden_by")
        assert a.get("overridden_at")

    def test_override_unknown_user_404(self, admin):
        r = admin["client"].post(
            f"{BASE_URL}/api/admin/grn/override-block/does-not-exist",
            json={"reason": "test"},
        )
        assert r.status_code == 404


# ---------------- Approve block-check ----------------
class TestApproveBlockedCheck:
    def test_blocked_user_cannot_approve(self, blocked_staff):
        """Try to approve some arbitrary doc; expect 403 that names the blocked shipment.
        We don't need the shipment to exist - block check happens before lookup."""
        r = blocked_staff["client"].post(
            f"{BASE_URL}/api/admin/grn/OTHER-DOC-XYZ/approve",
            json={
                "supplier_doc_num": "test", "bill_date": "2026-01-01",
                "site_id": "P2", "warehouse_id": "WH1", "item_actual_qtys": [],
            },
        )
        assert r.status_code == 403, r.text
        detail = r.json().get("detail", "")
        assert "TEST-BLK-Q7YA7Z" in detail, f"Detail should name blocked shipment, got: {detail}"
        assert "blocked" in detail.lower()


# ---------------- Recheck endpoint existence / gating ----------------
class TestRecheckEndpoint:
    def test_recheck_unknown_shipment_returns_404(self, staff):
        r = staff["client"].post(f"{BASE_URL}/api/admin/grn/NO-SUCH-DOC/recheck-manual-gr")
        assert r.status_code == 404
