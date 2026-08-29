"""Iteration 139 - regression tests for the GRN fixes from iteration_138.

Covers:
  * retry-movement / retry-goods-receipt return 404 (not 500) for unknown doc codes
  * sap_sync_status == 'skipped' for a fake/nonexistent PO (distinct from 'pending')
  * approved shipment keeps per-item actual_qty persisted (needed for the read-only Actual Qty column)
"""
import os

import pytest
import requests
from dotenv import dotenv_values

frontend_env = dotenv_values("/app/frontend/.env")
base_url = os.environ.get("REACT_APP_BACKEND_URL") or frontend_env.get("REACT_APP_BACKEND_URL")
if not base_url:
    raise RuntimeError("REACT_APP_BACKEND_URL is missing")
BASE_URL = base_url.rstrip("/")

SESSION_COOKIE = "KGABHXazt4DGxMLppFGLi9bbLu25Z9m9"


@pytest.fixture(scope="module")
def client():
    s = requests.Session()
    s.cookies.set("vms_session", SESSION_COOKIE)
    s.headers.update({"Content-Type": "application/json"})
    return s


# --- FIX 1: retry endpoints must 404 for unknown doc codes -------------------
@pytest.mark.parametrize("endpoint", ["retry-goods-receipt", "retry-movement"])
def test_retry_unknown_doc_code_returns_404(client, endpoint):
    r = client.post(f"{BASE_URL}/api/admin/grn/NOPE123/{endpoint}", timeout=60)
    assert r.status_code == 404, f"expected 404, got {r.status_code}: {r.text[:300]}"
    body = r.json()
    assert isinstance(body.get("detail"), str) and body["detail"], body


@pytest.mark.parametrize("endpoint", ["retry-goods-receipt", "retry-movement"])
def test_retry_requires_auth(endpoint):
    r = requests.post(f"{BASE_URL}/api/admin/grn/NOPE123/{endpoint}", timeout=60)
    assert r.status_code in (401, 403), r.status_code


# --- FIX 3/4: approved TESTG3 state ----------------------------------------
def test_testg3_skipped_status_and_actual_qty_persisted(client):
    r = client.get(f"{BASE_URL}/api/admin/grn/lookup/TESTG3", timeout=60)
    assert r.status_code == 200, r.text[:300]
    doc = r.json()
    assert "_id" in doc and doc["_id"] == "TESTG3"
    assert doc["status"] == "approved", f"TESTG3 status={doc['status']}"
    # fake PO 99999003 must never report as posted
    assert doc.get("sap_sync_status") == "skipped", doc.get("sap_sync_status")
    assert doc.get("sap_movement_status") == "not_applicable", doc.get("sap_movement_status")
    for it in doc["items"]:
        assert it.get("actual_qty") is not None, f"actual_qty missing on item {it.get('item_number')}"
    assert doc.get("site_id") == "P7"
    assert doc.get("warehouse_id")
    assert doc.get("supplier_doc_num") == "TEST-INV-G3"


def test_testg3_reapprove_rejected(client):
    r = client.post(
        f"{BASE_URL}/api/admin/grn/TESTG3/approve",
        json={"supplier_doc_num": "X", "bill_date": "2026-07-01", "site_id": "P7",
              "warehouse_id": "P7-JW", "item_actual_qtys": []},
        timeout=90,
    )
    assert r.status_code == 400, f"{r.status_code}: {r.text[:300]}"
