"""Backend tests for Supplier Additional Documents feature (iteration_146).
Covers:
  - Vendor upload/list/version retrieval + revisioning behavior
  - Invalid doc_type validation
  - Admin permission boundaries:
      * supplier_portal_admin can list + approve/reject
      * supplier_portal_documents-only can list but CANNOT approve/reject
      * neither -> 403 on list
  - Admin document retrieval for both doc types
"""
import io
import os
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://sap-data-sync.preview.emergentagent.com").rstrip("/")

# Backend is heavily loaded (SAP background jobs timing out), so
# request timeouts here are intentionally generous.
T = 120

VENDOR_EMAIL = "vendor1@testco.com"
VENDOR_PASSWORD = "DummyTest123"

DOCS_ONLY_COOKIE = "iter146docsonly000000000000000000000000000000"
ADMIN_COOKIE = "iter146supplieradmin000000000000000000000000"
NONE_COOKIE = "iter146noaccess00000000000000000000000000000"


@pytest.fixture(scope="module")
def vendor_session():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/supplier-portal/login",
               json={"email": VENDOR_EMAIL, "password": VENDOR_PASSWORD}, timeout=T)
    if r.status_code != 200:
        pytest.skip(f"Cannot log in supplier test vendor: {r.status_code} {r.text}")
    return s


@pytest.fixture(scope="module")
def vendor_account_id(vendor_session):
    r = vendor_session.get(f"{BASE_URL}/api/supplier-portal/me", timeout=T)
    assert r.status_code == 200, r.text
    return r.json()["account_id"]


# ---------- Vendor-facing upload/list/history ----------

def test_list_docs_initially(vendor_session):
    r = vendor_session.get(f"{BASE_URL}/api/supplier-portal/documents", timeout=T)
    assert r.status_code == 200, r.text
    data = r.json()
    assert "msme" in data and "bank" in data
    for k in ("msme", "bank"):
        assert set(data[k].keys()) >= {"latest", "history"}


def test_upload_msme_v1_and_verify(vendor_session):
    file_bytes = b"MSME v1 fixture content %d" % os.getpid()
    r = vendor_session.post(
        f"{BASE_URL}/api/supplier-portal/documents/msme",
        files={"file": ("msme_v1.pdf", io.BytesIO(file_bytes), "application/pdf")},
        timeout=T,
    )
    assert r.status_code == 200, r.text
    rev = r.json()
    assert rev["version"] >= 1
    assert rev["filename"] == "msme_v1.pdf"

    listing = vendor_session.get(f"{BASE_URL}/api/supplier-portal/documents", timeout=T).json()
    assert listing["msme"]["latest"]["filename"] == "msme_v1.pdf"
    latest_v = listing["msme"]["latest"]["version"]

    # Download the latest by version
    r2 = vendor_session.get(f"{BASE_URL}/api/supplier-portal/documents/msme/{latest_v}", timeout=T)
    assert r2.status_code == 200
    assert r2.content == file_bytes


def test_upload_msme_v2_creates_revision_and_v1_still_downloadable(vendor_session):
    initial = vendor_session.get(f"{BASE_URL}/api/supplier-portal/documents", timeout=T).json()
    prev_len = len(initial["msme"]["history"])
    prev_latest_v = initial["msme"]["latest"]["version"]

    file_bytes = b"MSME v2 fixture content %d" % os.getpid()
    r = vendor_session.post(
        f"{BASE_URL}/api/supplier-portal/documents/msme",
        files={"file": ("msme_v2.pdf", io.BytesIO(file_bytes), "application/pdf")},
        timeout=T,
    )
    assert r.status_code == 200, r.text
    assert r.json()["version"] == prev_latest_v + 1

    listing = vendor_session.get(f"{BASE_URL}/api/supplier-portal/documents", timeout=T).json()
    assert len(listing["msme"]["history"]) == prev_len + 1
    assert listing["msme"]["latest"]["filename"] == "msme_v2.pdf"

    # v1 must still be downloadable (never deleted)
    r_v1 = vendor_session.get(f"{BASE_URL}/api/supplier-portal/documents/msme/{prev_latest_v}", timeout=T)
    assert r_v1.status_code == 200
    assert r_v1.content == b"MSME v1 fixture content %d" % os.getpid()


def test_upload_bank_independent_of_msme(vendor_session):
    file_bytes = b"BANK v1 fixture %d" % os.getpid()
    r = vendor_session.post(
        f"{BASE_URL}/api/supplier-portal/documents/bank",
        files={"file": ("bank_v1.pdf", io.BytesIO(file_bytes), "application/pdf")},
        timeout=T,
    )
    assert r.status_code == 200, r.text
    listing = vendor_session.get(f"{BASE_URL}/api/supplier-portal/documents", timeout=T).json()
    assert listing["bank"]["latest"]["filename"] == "bank_v1.pdf"


def test_invalid_doc_type_rejected(vendor_session):
    r = vendor_session.post(
        f"{BASE_URL}/api/supplier-portal/documents/gst",  # not in ADDITIONAL_DOC_TYPES
        files={"file": ("x.pdf", io.BytesIO(b"x"), "application/pdf")},
        timeout=T,
    )
    assert r.status_code == 400, r.text


def test_get_nonexistent_version_returns_404(vendor_session):
    r = vendor_session.get(f"{BASE_URL}/api/supplier-portal/documents/msme/999", timeout=T)
    assert r.status_code == 404


def test_unauthenticated_documents_returns_401():
    r = requests.get(f"{BASE_URL}/api/supplier-portal/documents", timeout=T)
    assert r.status_code == 401


# ---------- Admin-side permission boundary tests ----------

def _cookies(token):
    return {"vms_session": token}


def test_admin_can_list_accounts():
    r = requests.get(f"{BASE_URL}/api/admin/supplier-portal/accounts",
                     cookies=_cookies(ADMIN_COOKIE), params={"status": "approved"}, timeout=T)
    assert r.status_code == 200, r.text
    data = r.json()
    assert "accounts" in data


def test_docs_only_user_can_list_accounts():
    r = requests.get(f"{BASE_URL}/api/admin/supplier-portal/accounts",
                     cookies=_cookies(DOCS_ONLY_COOKIE), params={"status": "approved"}, timeout=T)
    assert r.status_code == 200, r.text


def test_no_permission_user_cannot_list_accounts():
    r = requests.get(f"{BASE_URL}/api/admin/supplier-portal/accounts",
                     cookies=_cookies(NONE_COOKIE), params={"status": "approved"}, timeout=T)
    assert r.status_code == 403, r.text


def test_docs_only_user_CANNOT_approve(vendor_account_id):
    r = requests.post(
        f"{BASE_URL}/api/admin/supplier-portal/{vendor_account_id}/approve",
        cookies=_cookies(DOCS_ONLY_COOKIE), timeout=T,
    )
    assert r.status_code == 403, f"Expected 403, got {r.status_code}: {r.text}"


def test_docs_only_user_CANNOT_reject(vendor_account_id):
    r = requests.post(
        f"{BASE_URL}/api/admin/supplier-portal/{vendor_account_id}/reject",
        cookies=_cookies(DOCS_ONLY_COOKIE), json={"reason": "test"}, timeout=T,
    )
    assert r.status_code == 403, f"Expected 403, got {r.status_code}: {r.text}"


def test_admin_and_docs_only_can_view_msme_document(vendor_account_id):
    for name, cookie in [("admin", ADMIN_COOKIE), ("docs_only", DOCS_ONLY_COOKIE)]:
        r = requests.get(
            f"{BASE_URL}/api/admin/supplier-portal/{vendor_account_id}/documents/msme",
            cookies=_cookies(cookie), timeout=T, allow_redirects=False,
        )
        assert r.status_code == 200, f"{name}: {r.status_code} {r.text[:200]}"


def test_admin_supplier_portal_documents_permission_present_in_catalog():
    """The Access Management page reads from /api/admin/pages; the new
    supplier_portal_documents key must appear there."""
    # Need a super_admin or admin session - use ADMIN_COOKIE (only page-user, not admin role)
    # Since admin/pages requires admin role, this will 403 for our synthetic user.
    # Instead just probe with vendor for basic wiring; the real test is that keys are known.
    # (Enforced elsewhere: PAGE_KEYS set in auth_service will error on unknown keys during PUT.)
    import importlib.util, sys, pathlib
    backend_dir = pathlib.Path("/app/backend")
    sys.path.insert(0, str(backend_dir))
    from dotenv import load_dotenv
    load_dotenv(backend_dir / ".env")
    spec = importlib.util.spec_from_file_location("auth_service_probe", backend_dir / "auth_service.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    keys = {p["key"] for p in mod.PAGE_CATALOG}
    assert "supplier_portal_documents" in keys
    # Route rule: /api/admin/supplier-portal must accept EITHER key
    rule = next((pages for prefix, pages in mod.PAGE_ROUTE_RULES if prefix == "/api/admin/supplier-portal"), None)
    assert rule is not None
    assert "supplier_portal_admin" in rule
    assert "supplier_portal_documents" in rule
