"""Sep 4 2026 session (iteration_100): PO Bill-To per-site Finance code validation
+ Created POs history read endpoint.

SAFETY: this SAP tenant is LIVE PRODUCTION. These tests NEVER send a valid
(supplier, product, bill_to) combination to POST /api/purchase-orders/create -
only combinations that are rejected by FastAPI validation BEFORE any SAP call.
"""
import os

import pytest
import requests
from dotenv import dotenv_values

frontend_env = dotenv_values("/app/frontend/.env")
base_url = os.environ.get("REACT_APP_BACKEND_URL") or frontend_env.get("REACT_APP_BACKEND_URL")
if not base_url:
    raise RuntimeError("REACT_APP_BACKEND_URL missing")
BASE_URL = base_url.rstrip("/")

# Synthetic vms_session for po.tester@rampgroup.co.in (allowed_pages=['purchase_order'])
PO_SESSION = "aXpWl7XNHyKOvLVHeH4fh9I35zpQXhbUmDKPMAd1o5I"

RI_BILL_TO = ["P1-FIN", "P8-FIN", "P1W-FIN", "P5-FIN"]
RT_BILL_TO = ["P2-FIN", "P3-FIN", "P2W-FIN", "P7-FIN", "P9-FIN", "P4-FIN"]


@pytest.fixture(scope="module")
def client():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    s.cookies.set("vms_session", PO_SESSION)
    return s


def _fake_payload(site, bill_to):
    """Obviously-fake supplier/product so nothing can ever post to real SAP."""
    return {
        "supplier_code": "ZZZTEST999",
        "purchase_unit_site": site,
        "bill_to_company": bill_to,
        "po_date": "2026-09-10",
        "currency": "INR",
        "items": [{
            "product_id": "ZZZNOTREAL", "quantity": 1, "unit_of_measure": "EA",
            "unit_price": 1, "delivery_date": "2026-09-11",
        }],
    }


# --- GET /api/purchase-orders/history (Created POs page data source) ---
class TestPurchaseOrderHistory:
    def test_history_requires_auth(self):
        r = requests.get(f"{BASE_URL}/api/purchase-orders/history")
        assert r.status_code == 401

    def test_history_returns_array_with_expected_fields(self, client):
        r = client.get(f"{BASE_URL}/api/purchase-orders/history", params={"limit": 100})
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        assert len(data) > 0, "history is empty - Created POs table would render empty state"
        po = data[0]
        for f in ["po_number", "supplier_code", "purchase_unit_site", "bill_to_company",
                  "po_date", "items", "created_by", "created_at"]:
            assert f in po, f"missing field {f}"
        assert isinstance(po["items"], list) and len(po["items"]) >= 1
        assert "raw_xml" not in po
        # sorted newest first
        assert data == sorted(data, key=lambda d: d["created_at"], reverse=True)

    def test_history_limit_respected(self, client):
        r = client.get(f"{BASE_URL}/api/purchase-orders/history", params={"limit": 1})
        assert r.status_code == 200
        assert len(r.json()) == 1


# --- Bill-To validation on POST /api/purchase-orders/create (pre-SAP) ---
class TestBillToValidation:
    def test_sites_endpoint(self, client):
        r = client.get(f"{BASE_URL}/api/purchase-orders/sites")
        assert r.status_code == 200
        assert isinstance(r.json()["sites"], list) and "P1" in r.json()["sites"]

    def test_rt_billto_rejected_for_ri_site(self, client):
        r = client.post(f"{BASE_URL}/api/purchase-orders/create", json=_fake_payload("P1", "P2-FIN"))
        assert r.status_code == 400
        detail = r.json()["detail"]
        assert "Bill-To must be one of" in detail and "RI" in detail
        for opt in RI_BILL_TO:
            assert opt in detail

    def test_ri_billto_rejected_for_rt_site(self, client):
        r = client.post(f"{BASE_URL}/api/purchase-orders/create", json=_fake_payload("P2", "P1-FIN"))
        assert r.status_code == 400
        detail = r.json()["detail"]
        assert "RT" in detail
        for opt in RT_BILL_TO:
            assert opt in detail

    def test_legacy_bare_company_code_rejected(self, client):
        for site, comp in [("P1", "RI"), ("P2", "RT")]:
            r = client.post(f"{BASE_URL}/api/purchase-orders/create", json=_fake_payload(site, comp))
            assert r.status_code == 400, f"legacy bare '{comp}' should no longer be accepted"

    def test_garbage_billto_rejected(self, client):
        r = client.post(f"{BASE_URL}/api/purchase-orders/create", json=_fake_payload("P1", "NOT-A-CODE"))
        assert r.status_code == 400

    def test_bad_dates_rejected_before_sap(self, client):
        p = _fake_payload("P1", "P1-FIN")
        p["items"][0]["delivery_date"] = "2026-09-01"  # before po_date
        r = client.post(f"{BASE_URL}/api/purchase-orders/create", json=p)
        assert r.status_code == 422
