"""iter159 - Activate Material Site page backend tests.

Covers:
 - GET /api/admin/material-sites/lookup/{id} - happy path (PALL-373518)
 - Lookup: negative case (nonexistent material -> 404)
 - Access control: unauth (401) and limited user without permission (403)
 - Sites list endpoint (source of dropdown)
 - Idempotent activation POST against a site already active for the
   material (P1 for PALL-373518) - safe no-op per server's existing
   'already exists' -> ok mapping.
"""
import os
import pytest
import requests

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")

SUPER_COOKIE = "vqMBzf7pZcwK0THEYxUR8bKTsNZu3qRzGbY9GhQ9rKU"
LIMITED_COOKIE = "limited_ams_test_XpZImVh6aKQ"


def _client(cookie=None):
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    if cookie:
        s.cookies.set("vms_session", cookie)
    return s


@pytest.fixture
def super_client():
    return _client(SUPER_COOKIE)


@pytest.fixture
def limited_client():
    return _client(LIMITED_COOKIE)


@pytest.fixture
def anon_client():
    return _client()


# ---------------------- Lookup: happy path ----------------------
def test_lookup_pall_373518_happy_path(super_client):
    r = super_client.get(f"{BASE_URL}/api/admin/material-sites/lookup/PALL-373518", timeout=60)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data.get("uuid"), "expected uuid in response"
    assert "PALLET" in (data.get("description") or "").upper() or "PALL" in (data.get("description") or "").upper()
    assert data.get("product_category_id") == "RM"
    sites = data.get("active_sites") or []
    assert isinstance(sites, list) and len(sites) >= 3
    for expected in ("P1", "P2", "P4", "P7"):
        assert expected in sites, f"expected {expected} in active_sites, got {sites}"


# ---------------------- Lookup: negative ----------------------
def test_lookup_nonexistent_material_returns_404(super_client):
    r = super_client.get(f"{BASE_URL}/api/admin/material-sites/lookup/NONEXISTENT-MATERIAL-999", timeout=60)
    assert r.status_code == 404, r.text
    body = r.json()
    assert "detail" in body


# ---------------------- Access control ----------------------
def test_lookup_unauthenticated_403(anon_client):
    r = anon_client.get(f"{BASE_URL}/api/admin/material-sites/lookup/PALL-373518", timeout=30)
    assert r.status_code in (401, 403), r.text


def test_lookup_limited_user_forbidden(limited_client):
    r = limited_client.get(f"{BASE_URL}/api/admin/material-sites/lookup/PALL-373518", timeout=30)
    assert r.status_code == 403, r.text


def test_activate_unauthenticated_403(anon_client):
    r = anon_client.post(
        f"{BASE_URL}/api/admin/material-sites/activate",
        json={"product_id": "PALL-373518", "site_id": "P1"},
        timeout=30,
    )
    assert r.status_code in (401, 403), r.text


def test_activate_limited_user_forbidden(limited_client):
    r = limited_client.post(
        f"{BASE_URL}/api/admin/material-sites/activate",
        json={"product_id": "PALL-373518", "site_id": "P1"},
        timeout=30,
    )
    assert r.status_code == 403, r.text


# ---------------------- Sites list (dropdown source) ----------------------
def test_sites_list_available(super_client):
    r = super_client.get(f"{BASE_URL}/api/purchase-orders/sites", timeout=30)
    assert r.status_code == 200, r.text
    sites = r.json().get("sites", [])
    assert isinstance(sites, list) and len(sites) > 0
    # A few well-known ones expected
    for s in ("P1", "P2"):
        assert s in sites, f"expected {s} in sites list {sites}"


# ---------------------- Activation idempotent safe path (P1 already active) ----------------------
def test_activate_already_active_site_is_safe_noop(super_client):
    """PALL-373518 is already active at P1 - server maps SAP's 'already
    exists' response to ok, so this is a safe no-op and must return
    planning_logistics=='ok' and valuation=='ok'."""
    r = super_client.post(
        f"{BASE_URL}/api/admin/material-sites/activate",
        json={"product_id": "PALL-373518", "site_id": "P1"},
        timeout=180,
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data.get("planning_logistics") == "ok", f"expected planning_logistics=ok, got {data}"
    assert data.get("valuation") == "ok", f"expected valuation=ok (already active), got {data}"
