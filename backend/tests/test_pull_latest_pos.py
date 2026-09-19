"""Backend tests for the new Pull Latest POs endpoints (Sep 20 2026 refactor).

Covers:
- POST /api/supplier-portal/purchase-orders/refresh (supplier session cookie)
- POST /api/admin/act-as-supplier/{account_id}/purchase-orders/refresh (admin cookie)
- Regression: the OLD unscoped act-as-supplier refresh URL is gone (404).
"""
import os
import time
import pytest
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL") or open("/app/frontend/.env").read().split("REACT_APP_BACKEND_URL=")[1].split("\n")[0].strip()
BASE_URL = BASE_URL.rstrip("/")

SUPPLIER_EMAIL = "test@test.com"
SUPPLIER_PASSWORD = "Test123"
ADMIN_COOKIE_ACT_AS = "iter183actassupplierpos000000000000000000000000"


@pytest.fixture(scope="module")
def supplier_session():
    s = requests.Session()
    last_err = None
    for _ in range(5):
        try:
            r = s.post(f"{BASE_URL}/api/supplier-portal/login",
                       json={"email": SUPPLIER_EMAIL, "password": SUPPLIER_PASSWORD}, timeout=90)
            if r.status_code == 200:
                return s
            last_err = AssertionError(f"login {r.status_code} {r.text[:200]}")
            time.sleep(4)
        except requests.exceptions.Timeout as e:
            last_err = e
            time.sleep(2)
    raise last_err


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    s.cookies.set("vms_session", ADMIN_COOKIE_ACT_AS)
    retry = Retry(total=5, backoff_factor=1.5, status_forcelist=[502, 503, 504], allowed_methods=["GET", "POST"])
    s.mount("https://", HTTPAdapter(max_retries=retry))
    # Store default timeout via wrapper
    _orig = s.request
    def _req(method, url, **kw):
        kw.setdefault("timeout", 90)
        return _orig(method, url, **kw)
    s.request = _req
    return s


def test_supplier_pull_latest_pos(supplier_session):
    t0 = time.time()
    r = supplier_session.post(f"{BASE_URL}/api/supplier-portal/purchase-orders/refresh", timeout=90)
    elapsed = time.time() - t0
    print(f"supplier refresh took {elapsed:.2f}s -> {r.status_code}")
    assert r.status_code == 200, f"{r.status_code} {r.text[:500]}"
    data = r.json()
    assert data.get("status") == "done"
    assert isinstance(data.get("po_count"), int)
    assert elapsed < 60, f"took too long: {elapsed}s"


def test_supplier_dashboard_lists_pos_after_pull(supplier_session):
    r = supplier_session.get(f"{BASE_URL}/api/supplier-portal/purchase-orders", timeout=30)
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, dict)
    assert "purchase_orders" in data
    assert isinstance(data["purchase_orders"], list)
    print(f"supplier open PO count: {len(data['purchase_orders'])}, live_sync={data.get('live_sync')}")


def test_old_unscoped_act_as_supplier_refresh_is_gone(admin_session):
    # Old routes we would expect to be gone. Try a few plausible old shapes.
    old_urls = [
        f"{BASE_URL}/api/admin/act-as-supplier/purchase-orders/refresh",
        f"{BASE_URL}/api/admin/act-as-supplier/purchase-orders/pull-latest",
    ]
    for u in old_urls:
        r = admin_session.post(u, timeout=15)
        print(f"OLD {u} -> {r.status_code}")
        assert r.status_code in (404, 405), f"old URL {u} unexpectedly returned {r.status_code}"


def test_admin_act_as_supplier_refresh(admin_session):
    # First find an account_id
    r = admin_session.get(f"{BASE_URL}/api/admin/act-as-supplier/accounts", timeout=30)
    assert r.status_code == 200, f"list accounts: {r.status_code} {r.text[:300]}"
    accounts = r.json()
    if isinstance(accounts, dict):
        accounts = accounts.get("accounts", [])
    assert accounts, "no supplier accounts to test against"
    account_id = accounts[0].get("_id") or accounts[0].get("id")
    vendor_code = accounts[0].get("vendor_code")
    print(f"using account {account_id} vendor {vendor_code}")

    t0 = time.time()
    r = admin_session.post(
        f"{BASE_URL}/api/admin/act-as-supplier/{account_id}/purchase-orders/refresh",
        timeout=90,
    )
    elapsed = time.time() - t0
    print(f"admin refresh took {elapsed:.2f}s -> {r.status_code} body: {r.text[:200]}")
    assert r.status_code == 200, f"{r.status_code} {r.text[:500]}"
    data = r.json()
    assert data.get("status") == "done"
    assert isinstance(data.get("po_count"), int)
    assert elapsed < 60


def test_admin_act_as_supplier_refresh_bad_account_id(admin_session):
    r = admin_session.post(
        f"{BASE_URL}/api/admin/act-as-supplier/nonexistent-id-xyz/purchase-orders/refresh",
        timeout=30,
    )
    assert r.status_code == 404, f"expected 404, got {r.status_code} {r.text[:300]}"


# Regression: after Pull, supplier can still create a shipment against a cached PO row.
# This verifies fetch_pos_for_vendor cache rows are schema-compatible with shipment creation.
def test_supplier_can_create_shipment_after_pull(supplier_session):
    # Grab current open POs
    r = supplier_session.get(f"{BASE_URL}/api/supplier-portal/purchase-orders", timeout=30)
    assert r.status_code == 200
    data = r.json()
    pos = data.get("purchase_orders", [])
    assert pos, "no open POs to test shipment creation with"

    # Find first PO row with open qty > 0
    row = None
    for p in pos:
        open_qty = float(p.get("open_qty") or 0)
        if open_qty > 0:
            row = p
            break
    assert row, "no PO row with open_qty > 0"
    print(f"Using PO {row['po_number']} item {row.get('item_number')} open_qty={row.get('open_qty')}")

    ship_qty = min(1.0, float(row["open_qty"]))
    payload = {"items": [{
        "po_number": row["po_number"],
        "item_number": str(row["item_number"]),
        "ship_qty": ship_qty,
    }]}
    resp = supplier_session.post(f"{BASE_URL}/api/supplier-portal/shipments", json=payload, timeout=60)
    print(f"shipment create -> {resp.status_code} {resp.text[:400]}")
    assert resp.status_code in (200, 201), f"shipment create failed: {resp.status_code} {resp.text[:500]}"
    body = resp.json()
    shipment_id = body.get("_id") or body.get("id") or body.get("shipment_id")
    assert shipment_id, f"no shipment id in response: {body}"

    # Verify shipment is retrievable
    g = supplier_session.get(f"{BASE_URL}/api/supplier-portal/shipments", timeout=30)
    assert g.status_code == 200
    shipments = g.json()
    if isinstance(shipments, dict):
        shipments = shipments.get("shipments", shipments.get("data", []))
    ids = [s.get("_id") or s.get("id") for s in shipments]
    assert shipment_id in ids, f"created shipment {shipment_id} not in list"
    print(f"created shipment id={shipment_id} verified in list")
