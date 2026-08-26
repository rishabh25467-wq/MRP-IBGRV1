"""Supplier Portal (external vendor onboarding/auth) - Phase 1+2 tests.

Covers: signup (multipart, validation), login while pending, PO 403/503,
admin approval routes (page-permission gated), document download,
approve/reject, logout, and internal-auth regression checks.
"""
import io
import os
import time
import uuid

import pytest
import requests
from dotenv import dotenv_values

frontend_env = dotenv_values("/app/frontend/.env")
base_url = os.environ.get("REACT_APP_BACKEND_URL") or frontend_env.get("REACT_APP_BACKEND_URL")
if not base_url:
    raise RuntimeError("REACT_APP_BACKEND_URL missing")
BASE_URL = base_url.rstrip("/")
API = f"{BASE_URL}/api"

ADMIN_SESSION_COOKIE = "_nNTeagpGyLWuMhFLKHF2qvr0lfmKm8P"  # synthetic Entra session w/ supplier_portal_admin


def _files():
    return {
        "gst_doc": ("gst_cert.pdf", io.BytesIO(b"%PDF-1.4 TEST GST CERT"), "application/pdf"),
        "pan_doc": ("pan_card.png", io.BytesIO(b"\x89PNG\r\n\x1a\nTESTPAN"), "image/png"),
    }


def _signup_data(email, password="Passw0rd123"):
    return {
        "vendor_code": "S9001",
        "company_name": "TEST_Vendor_Signup Co",
        "email": email,
        "password": password,
        "gst_number": "27AABCT1234A1Z5",
        "pan_number": "AABCT1234A",
    }


@pytest.fixture(scope="module")
def admin_client():
    s = requests.Session()
    s.cookies.set("vms_session", ADMIN_SESSION_COOKIE, domain=BASE_URL.split("//")[1])
    return s


@pytest.fixture(scope="module")
def new_vendor_email():
    return f"test_vendor_{uuid.uuid4().hex[:8]}@testco.com"


@pytest.fixture(scope="module")
def created_ids():
    return []


@pytest.fixture(scope="module", autouse=True)
def cleanup(created_ids):
    yield
    # cleanup handled by direct mongo call at end of module
    if not created_ids:
        return
    try:
        import sys
        sys.path.insert(0, "/app/backend")
        from pymongo import MongoClient
        env = dotenv_values("/app/backend/.env")
        client = MongoClient(env["MONGO_URL"])
        db = client[env["DB_NAME"]]
        db["supplier_portal_accounts"].delete_many({"_id": {"$in": created_ids}})
    except Exception as e:
        print(f"cleanup skipped: {e}")


# ---------- unauthenticated /me ----------
class TestSupplierMe:
    def test_me_logged_out_returns_200_not_authenticated(self):
        r = requests.get(f"{API}/supplier-portal/me")
        assert r.status_code == 200, r.text
        assert r.json() == {"authenticated": False}

    def test_me_with_garbage_token_returns_200(self):
        r = requests.get(f"{API}/supplier-portal/me", headers={"Authorization": "Bearer garbage.token.here"})
        assert r.status_code == 200
        assert r.json().get("authenticated") is False


# ---------- signup validation ----------
class TestSignupValidation:
    def test_missing_text_field_rejected(self):
        data = _signup_data("x@y.com")
        del data["vendor_code"]
        r = requests.post(f"{API}/supplier-portal/signup", data=data, files=_files())
        assert r.status_code == 422, r.text

    def test_blank_text_field_rejected_400(self):
        data = _signup_data(f"blank_{uuid.uuid4().hex[:6]}@testco.com")
        data["vendor_code"] = "   "
        r = requests.post(f"{API}/supplier-portal/signup", data=data, files=_files())
        assert r.status_code == 400, r.text
        assert "required" in r.json()["detail"].lower()

    def test_missing_file_rejected(self):
        files = _files()
        del files["pan_doc"]
        r = requests.post(f"{API}/supplier-portal/signup", data=_signup_data("x2@y.com"), files=files)
        assert r.status_code == 422, r.text

    def test_short_password_rejected(self):
        r = requests.post(f"{API}/supplier-portal/signup",
                          data=_signup_data(f"short_{uuid.uuid4().hex[:6]}@testco.com", password="abc123"),
                          files=_files())
        assert r.status_code == 400, r.text
        assert "8 characters" in r.json()["detail"]

    def test_duplicate_email_rejected(self):
        r = requests.post(f"{API}/supplier-portal/signup",
                          data=_signup_data("vendor1@testco.com"), files=_files())
        assert r.status_code == 400, r.text
        assert "already exists" in r.json()["detail"].lower()


# ---------- full pending -> approve pipeline ----------
class TestSignupApprovePipeline:
    def test_01_signup_success(self, new_vendor_email, created_ids):
        r = requests.post(f"{API}/supplier-portal/signup", data=_signup_data(new_vendor_email), files=_files())
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "pending_approval"
        assert isinstance(body["account_id"], str) and body["account_id"]
        created_ids.append(body["account_id"])

    def test_02_login_while_pending_works(self, new_vendor_email):
        s = requests.Session()
        r = s.post(f"{API}/supplier-portal/login", json={"email": new_vendor_email, "password": "Passw0rd123"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "pending"
        assert body["authenticated"] is True
        assert body["email"] == new_vendor_email
        assert "supplier_token" in s.cookies.get_dict()
        # pending vendor gets 403 on POs
        po = s.get(f"{API}/supplier-portal/purchase-orders")
        assert po.status_code == 403, po.text

    def test_03_wrong_password_401(self, new_vendor_email):
        r = requests.post(f"{API}/supplier-portal/login", json={"email": new_vendor_email, "password": "WrongPass999"})
        assert r.status_code == 401, r.text
        assert r.json()["detail"] == "Invalid email or password"

    def test_04_admin_lists_pending(self, admin_client, new_vendor_email, created_ids):
        r = admin_client.get(f"{API}/admin/supplier-portal/accounts", params={"status": "pending"})
        assert r.status_code == 200, r.text
        accounts = r.json()["accounts"]
        match = [a for a in accounts if a["email"] == new_vendor_email]
        assert match, f"new vendor not in pending list: {[a['email'] for a in accounts]}"
        acct = match[0]
        assert acct["gst_number"] == "27AABCT1234A1Z5"
        assert acct["pan_number"] == "AABCT1234A"
        assert "password_hash" not in acct
        assert acct.get("_id") == created_ids[0]

    def test_05_admin_downloads_documents(self, admin_client, created_ids):
        aid = created_ids[0]
        gst = admin_client.get(f"{API}/admin/supplier-portal/{aid}/documents/gst")
        assert gst.status_code == 200, gst.text
        assert gst.content == b"%PDF-1.4 TEST GST CERT"
        assert "pdf" in gst.headers.get("content-type", "")
        pan = admin_client.get(f"{API}/admin/supplier-portal/{aid}/documents/pan")
        assert pan.status_code == 200, pan.text
        assert pan.content == b"\x89PNG\r\n\x1a\nTESTPAN"

    def test_06_invalid_doc_type_400(self, admin_client, created_ids):
        r = admin_client.get(f"{API}/admin/supplier-portal/{created_ids[0]}/documents/aadhar")
        assert r.status_code == 400, r.text

    def test_07_reject_then_login_shows_reason(self, admin_client, created_ids, new_vendor_email):
        aid = created_ids[0]
        r = admin_client.post(f"{API}/admin/supplier-portal/{aid}/reject", json={"reason": "TEST_ docs unreadable"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "rejected"
        lst = admin_client.get(f"{API}/admin/supplier-portal/accounts", params={"status": "rejected"}).json()["accounts"]
        acct = [a for a in lst if a["_id"] == aid][0]
        assert acct["rejection_reason"] == "TEST_ docs unreadable"
        # Rejected vendor CAN authenticate (intentional since iteration_120) but is
        # surfaced status=rejected + reason; data routes stay gated on approved.
        s = requests.Session()
        login = s.post(f"{API}/supplier-portal/login",
                       json={"email": new_vendor_email, "password": "Passw0rd123"})
        assert login.status_code == 200, login.text
        assert login.json()["status"] == "rejected"
        assert login.json()["rejection_reason"] == "TEST_ docs unreadable"
        po = s.get(f"{API}/supplier-portal/purchase-orders")
        assert po.status_code == 403, f"rejected vendor must not read POs: {po.status_code} {po.text[:200]}"

    def test_08_approve_moves_out_of_pending(self, admin_client, created_ids, new_vendor_email):
        aid = created_ids[0]
        r = admin_client.post(f"{API}/admin/supplier-portal/{aid}/approve")
        assert r.status_code == 200, r.text
        pend = admin_client.get(f"{API}/admin/supplier-portal/accounts", params={"status": "pending"}).json()["accounts"]
        assert aid not in [a["_id"] for a in pend]
        appr = admin_client.get(f"{API}/admin/supplier-portal/accounts", params={"status": "approved"}).json()["accounts"]
        acct = [a for a in appr if a["_id"] == aid][0]
        assert acct["status"] == "approved"
        assert acct["rejection_reason"] is None
        assert acct["approved_by"]

    def test_09_approved_vendor_login_and_po_cached_fallback(self, new_vendor_email):
        s = requests.Session()
        r = s.post(f"{API}/supplier-portal/login", json={"email": new_vendor_email, "password": "Passw0rd123"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "approved"
        po = s.get(f"{API}/supplier-portal/purchase-orders")
        # Phase 3: SAP PO endpoint unconfigured -> graceful cached fallback, not 503
        assert po.status_code == 200, po.text
        body = po.json()
        assert body["live_sync"] is False
        assert isinstance(body["purchase_orders"], list)

    def test_10_logout_clears_session(self, new_vendor_email):
        s = requests.Session()
        s.post(f"{API}/supplier-portal/login", json={"email": new_vendor_email, "password": "Passw0rd123"})
        assert s.get(f"{API}/supplier-portal/me").json()["authenticated"] is True
        out = s.post(f"{API}/supplier-portal/logout")
        assert out.status_code == 200
        assert s.get(f"{API}/supplier-portal/me").json()["authenticated"] is False

    def test_11_po_requires_login(self):
        r = requests.get(f"{API}/supplier-portal/purchase-orders")
        assert r.status_code == 401, r.text


# ---------- brute force lockout ----------
class TestBruteForceLockout:
    def test_lockout_after_5_failed_attempts(self):
        email = f"lockout_{uuid.uuid4().hex[:8]}@testco.com"
        last = None
        for _ in range(5):
            last = requests.post(f"{API}/supplier-portal/login", json={"email": email, "password": "bad"})
            assert last.status_code == 401
            time.sleep(0.1)
        r = requests.post(f"{API}/supplier-portal/login", json={"email": email, "password": "bad"})
        assert r.status_code == 401
        assert "too many failed attempts" in r.json()["detail"].lower(), r.text


# ---------- internal auth regression ----------
class TestInternalAuthRegression:
    def test_logged_out_internal_route_401(self):
        # NOTE: /api/auth/me is intentionally public (returns authenticated:false)
        for path in ("/suppliers", "/inventory/sites", "/store-requests/known-sites"):
            r = requests.get(f"{API}{path}")
            assert r.status_code == 401, f"{path} -> {r.status_code} {r.text[:200]}"

    def test_admin_supplier_portal_requires_login(self):
        r = requests.get(f"{API}/admin/supplier-portal/accounts")
        assert r.status_code == 401, r.text

    def test_supplier_admin_session_denied_on_other_internal_pages(self, admin_client):
        # this synthetic account only has supplier_portal_admin
        r = admin_client.get(f"{API}/suppliers")
        assert r.status_code == 403, f"expected 403 for un-granted page, got {r.status_code}"

    def test_supplier_token_does_not_grant_internal_access(self):
        s = requests.Session()
        s.post(f"{API}/supplier-portal/login", json={"email": "vendor1@testco.com", "password": "Passw0rd123"})
        r = s.get(f"{API}/admin/supplier-portal/accounts")
        assert r.status_code == 401, f"supplier JWT must not authenticate internal admin routes: {r.status_code}"


# ---------- password hash format ----------
class TestPasswordHashing:
    def test_bcrypt_hash_format(self):
        import sys
        sys.path.insert(0, "/app/backend")
        from pymongo import MongoClient
        env = dotenv_values("/app/backend/.env")
        client = MongoClient(env["MONGO_URL"])
        db = client[env["DB_NAME"]]
        acct = db["supplier_portal_accounts"].find_one({"email": "vendor1@testco.com"})
        assert acct, "seeded approved vendor missing"
        assert acct["password_hash"].startswith("$2b$"), acct["password_hash"][:10]

    def test_login_sets_httponly_secure_cookie(self):
        r = requests.post(f"{API}/supplier-portal/login",
                          json={"email": "vendor1@testco.com", "password": "Passw0rd123"})
        assert r.status_code == 200, r.text
        set_cookie = r.headers.get("set-cookie", "")
        assert "supplier_token=" in set_cookie
        assert "HttpOnly" in set_cookie
        assert "Secure" in set_cookie
