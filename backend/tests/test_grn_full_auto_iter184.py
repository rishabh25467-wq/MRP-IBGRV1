"""iteration_184 - Backend tests for the new POST /admin/grn/{doc}/approve-full-auto
endpoint (Test Full Automated GRN button - EM1 SOAP breakthrough, site P8 only).

CRITICAL SAFETY: Only exercise NEGATIVE / rejection paths for approve-full-auto.
Never call it with a real valid P8 shipment - that triggers a real, irreversible
Goods Receipt in the customer's live SAP system.
"""
import os
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://sap-data-sync.preview.emergentagent.com").rstrip("/")
# Synthetic super_admin session seeded for iter184 (allowed_pages: vendor_goods_receipt,
# bound_sites: [] -> all sites), valid 7 days from Sep 18 2026.
SESSION_COOKIE = {"vms_session": "iter184grnfullauto0000000000000000000000000000"}
# Real in_transit shipment used only for negative-path tests.
DOC_CODE = "Q7YA7Z"


def _body(**overrides):
    b = {
        "supplier_doc_num": "TEST-INV-999",
        "bill_date": "2026-01-15",
        "site_id": "P1",
        "warehouse_id": "P1-RM",
        "item_actual_qtys": [{"po_number": "28792", "item_number": "2", "actual_qty": 1}],
    }
    b.update(overrides)
    return b


def test_full_auto_rejects_site_p1():
    r = requests.post(
        f"{BASE_URL}/api/admin/grn/{DOC_CODE}/approve-full-auto",
        cookies=SESSION_COOKIE, json=_body(site_id="P1", warehouse_id="P1-RM"),
    )
    assert r.status_code == 400, r.text
    assert "P8" in r.json()["detail"]
    assert "only set up for" in r.json()["detail"].lower()


def test_full_auto_rejects_site_p3():
    r = requests.post(
        f"{BASE_URL}/api/admin/grn/{DOC_CODE}/approve-full-auto",
        cookies=SESSION_COOKIE, json=_body(site_id="P3", warehouse_id="P3-RM"),
    )
    assert r.status_code == 400
    assert "P8" in r.json()["detail"]


def test_full_auto_requires_invoice_and_bill_date():
    r = requests.post(
        f"{BASE_URL}/api/admin/grn/{DOC_CODE}/approve-full-auto",
        cookies=SESSION_COOKIE,
        json=_body(site_id="P8", warehouse_id="P8-RM", supplier_doc_num="", bill_date=""),
    )
    assert r.status_code == 400
    assert "Supplier Invoice Number" in r.json()["detail"]


def test_full_auto_requires_auth():
    r = requests.post(
        f"{BASE_URL}/api/admin/grn/{DOC_CODE}/approve-full-auto",
        json=_body(),
    )
    assert r.status_code == 401
