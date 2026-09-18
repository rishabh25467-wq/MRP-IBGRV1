"""iter185 - verify /api/supplier-portal/purchase-orders endpoint responds
under ~12s for a large-PO vendor (Radish Technology RAD-P2-S) after the
Sep 19 2026 ThreadPoolExecutor parallelization of
sap_po_analytics_client.fetch_open_po_quantities.

Safe: read-only OData query, no SAP writes."""
import os
import time
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL").rstrip("/")
SUPPLIER_LOGIN_EMAIL = "vendor1@testco.com"
SUPPLIER_LOGIN_PASSWORD = "DummyTest123"
LARGE_VENDOR = "RAD-P2-S"
SPEED_TARGET_SECONDS = 12.0


@pytest.fixture(scope="module")
def supplier_session():
    s = requests.Session()
    r = s.post(
        f"{BASE_URL}/api/supplier-portal/login",
        json={"email": SUPPLIER_LOGIN_EMAIL, "password": SUPPLIER_LOGIN_PASSWORD},
        timeout=90,
    )
    if r.status_code != 200:
        pytest.skip(f"Supplier login failed (HTTP {r.status_code}): {r.text[:200]}")
    return s


def test_version_smoke(supplier_session):
    r = requests.get(f"{BASE_URL}/api/version", timeout=30)
    assert r.status_code == 200
    assert "commit" in r.json()


def test_supplier_portal_me_authenticated(supplier_session):
    r = supplier_session.get(f"{BASE_URL}/api/supplier-portal/me", timeout=30)
    assert r.status_code == 200
    data = r.json()
    assert data.get("email") == SUPPLIER_LOGIN_EMAIL
    assert data.get("testing_mode") is True, "testing_mode must be on to use as_vendor override"


def test_purchase_orders_for_large_vendor_under_12s(supplier_session):
    """Warm cache first so we measure the endpoint's steady-state speed
    (the very first hit does a cold SAP session establishment)."""
    supplier_session.get(
        f"{BASE_URL}/api/supplier-portal/purchase-orders",
        params={"as_vendor": LARGE_VENDOR},
        timeout=60,
    )
    # Measured run
    start = time.time()
    r = supplier_session.get(
        f"{BASE_URL}/api/supplier-portal/purchase-orders",
        params={"as_vendor": LARGE_VENDOR},
        timeout=60,
    )
    elapsed = time.time() - start
    assert r.status_code == 200, f"HTTP {r.status_code}: {r.text[:300]}"
    body = r.json()
    assert body.get("vendor_code") == LARGE_VENDOR
    assert isinstance(body.get("purchase_orders"), list)
    po_count = len(body["purchase_orders"])
    print(f"\n[iter185] {LARGE_VENDOR}: {po_count} POs, {elapsed:.2f}s (target < {SPEED_TARGET_SECONDS}s)")
    assert elapsed < SPEED_TARGET_SECONDS, (
        f"PO fetch took {elapsed:.2f}s for {po_count} POs (was 20-25s pre-parallelization, target < {SPEED_TARGET_SECONDS}s)"
    )
