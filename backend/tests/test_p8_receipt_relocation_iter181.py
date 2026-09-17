"""Iteration 181 tests for the P8-HOLD -> real target warehouse relocation
being MOVED from the outbound Complete-STO endpoint to the inbound Receive
endpoint (see review_request iteration 181).

Covers:
 - Regression: old /api/stock-transfer/orders/{sto_id}/retry-receipt-relocation is GONE.
 - New:        /api/inbound-receipts/{sto_id}/retry-receipt-relocation exists w/ validation.
 - New:        /api/inbound-receipts/completed exposes `receipt_relocation` field.
 - Business:   _relocate_receipt_from_hold surfaces per-line
               "Stock does not exist in the STO warehouse" for a product with 0 P8-HOLD stock,
               without calling SAP.
 - Regression: stock_transfer_service does not carry any P8-HOLD relocation code.
"""
import os
import sys
import uuid
import pytest
import requests
from datetime import datetime, timezone

sys.path.insert(0, "/app/backend")
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")
load_dotenv("/app/frontend/.env")
from pymongo import MongoClient

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]

# Reuse the pre-existing STO test session (see /app/memory/test_credentials.md
# "Manual Goods Issue for multi-line STOs - test session")
SESSION_COOKIE = "u-8qIcJoBrX8l6ZyV2JKWMte5t5GJsd_vzm6WzC1vqc"


@pytest.fixture(scope="module")
def db():
    client = MongoClient(MONGO_URL)
    return client[DB_NAME]


@pytest.fixture(scope="module")
def session_cookie(db):
    # Ensure session exists (re-seed if expired). Grants inbound_stock_transfer as well.
    from datetime import timedelta
    user_id = "test-tid:sto-manual-gi-oid"
    db.auth_users.update_one({"_id": user_id}, {"$set": {
        "tid": "test-tid", "oid": "sto-manual-gi-oid",
        "email": "sto.manualgi.test@rampgroup.co.in", "name": "STO Manual GI Tester",
        "role": "super_admin",
        "allowed_pages": ["stock_transfer", "inbound_stock_transfer"],
        "bound_sites": [],
    }}, upsert=True)
    db.auth_sessions.update_one({"_id": SESSION_COOKIE}, {"$set": {
        "user_id": user_id,
        "expires_at": datetime.now(timezone.utc) + timedelta(days=7),
    }}, upsert=True)
    return SESSION_COOKIE


@pytest.fixture
def api(session_cookie):
    s = requests.Session()
    s.cookies.set("vms_session", session_cookie)
    s.headers.update({"Content-Type": "application/json"})
    return s


# ---- Regression: old outbound endpoint gone ---------------------------------
def test_old_retry_receipt_relocation_endpoint_removed(api):
    r = api.post(f"{BASE_URL}/api/stock-transfer/orders/STO-FAKE-123/retry-receipt-relocation")
    assert r.status_code in (404, 405), f"Expected 404/405, got {r.status_code}: {r.text[:200]}"


def test_stock_transfer_service_has_no_relocation_symbols():
    import stock_transfer_service as sts
    for sym in ("_relocate_receipt_from_hold", "retry_receipt_relocation",
                "RECEIPT_RELOCATION_SITE_ID", "RECEIPT_RELOCATION_HOLD_WAREHOUSE_ID"):
        assert not hasattr(sts, sym), f"stock_transfer_service still exports {sym}"


def test_check_manual_gi_completion_signature_no_gm_client():
    import inspect
    import stock_transfer_service as sts
    sig = inspect.signature(sts.check_manual_gi_completion)
    assert "sap_goods_movement_client" not in sig.parameters


# ---- New: inbound endpoint exists + validates -------------------------------
def test_new_inbound_retry_endpoint_404_for_missing_sto(api):
    r = api.post(f"{BASE_URL}/api/inbound-receipts/STO-DOES-NOT-EXIST-XYZ/retry-receipt-relocation")
    # Not found produces 400 (ValueError->HTTPException 400 in server.py)
    assert r.status_code == 400
    assert "not found" in r.text.lower()


def test_new_inbound_retry_endpoint_400_when_not_received(api, db):
    sto_id = f"TEST_RELO_{uuid.uuid4().hex[:8]}"
    db.stock_transfer_orders.insert_one({
        "_id": sto_id, "gi_status": "posted",
        "receipt_status": "pending",
        "ship_to_site_id": "P8", "ship_to_location_id": "P8-RM",
        "items": [], "created_at": datetime.now(timezone.utc),
    })
    try:
        r = api.post(f"{BASE_URL}/api/inbound-receipts/{sto_id}/retry-receipt-relocation")
        assert r.status_code == 400
        assert "received" in r.text.lower()
    finally:
        db.stock_transfer_orders.delete_one({"_id": sto_id})


def test_new_inbound_retry_endpoint_400_when_non_p8(api, db):
    sto_id = f"TEST_RELO_{uuid.uuid4().hex[:8]}"
    db.stock_transfer_orders.insert_one({
        "_id": sto_id, "gi_status": "posted",
        "receipt_status": "received",
        "ship_to_site_id": "P1", "ship_to_location_id": "P1-RM",
        "items": [], "created_at": datetime.now(timezone.utc),
    })
    try:
        r = api.post(f"{BASE_URL}/api/inbound-receipts/{sto_id}/retry-receipt-relocation")
        assert r.status_code == 400
        assert "P8" in r.text
    finally:
        db.stock_transfer_orders.delete_one({"_id": sto_id})


# ---- Business logic: per-line stock check surfaces friendly error ----------
def test_relocate_returns_stock_not_exist_error_for_zero_hold_stock(db):
    """Seed an STO with a product_id known to have zero P8-HOLD stock and
    invoke _relocate_receipt_from_hold directly (bypasses HTTP). It must
    return per-line status='failed' with the exact friendly error text,
    WITHOUT invoking sap_goods_movement_client."""
    import inbound_receipt_service as irs

    class _FailingGM:
        def __getattr__(self, name):
            raise AssertionError(f"SAP GM client should NOT be called (called {name})")

    doc = {
        "_id": "TEST_RELO_STOCKCHK",
        "ship_to_site_id": "P8",
        "ship_to_location_id": "P8-RM",
        "items": [{
            "line_no": 1, "product_id": "NONEXISTENT_PRODUCT_ZZZ_181",
            "requested_qty": 5, "unit_of_measure": "EA",
        }],
    }
    result = irs._relocate_receipt_from_hold(db, _FailingGM(), doc)
    assert result["status"] == "failed", result
    assert result["lines"][0]["ok"] is False
    assert result["lines"][0]["error"] == "Stock does not exist in the STO warehouse"


def test_relocate_skips_same_warehouse(db):
    import inbound_receipt_service as irs
    doc = {"_id": "X", "ship_to_site_id": "P8", "ship_to_location_id": "P8-HOLD", "items": [{}]}
    result = irs._relocate_receipt_from_hold(db, None, doc)
    assert result == {"status": "skipped_same_warehouse"}


# ---- New: completed endpoint returns receipt_relocation field --------------
def test_completed_endpoint_returns_receipt_relocation_field(api, db):
    """Seed a completed STO with a fake receipt_relocation and verify roundtrip."""
    sto_id = f"TEST_RELO_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    fake_reloc = {"status": "done", "to": "P8-RM",
                  "lines": [{"product_id": "P1", "ok": True, "gac_id": "GAC-TEST"}]}
    db.stock_transfer_orders.insert_one({
        "_id": sto_id, "gi_status": "posted",
        "receipt_status": "received",
        "ship_from_site_id": "P2", "ship_to_site_id": "P8",
        "ship_to_location_id": "P8-RM", "ship_to_location_name": "P8-RM",
        "sap_order_id": "888888", "items": [],
        "receipt_completed_at": now, "received_at": now,
        "receipt_duration_seconds": 42, "receipt_relocation": fake_reloc,
        "created_at": now,
    })
    try:
        r = api.get(f"{BASE_URL}/api/inbound-receipts/completed?site_id=P8")
        assert r.status_code == 200, r.text
        orders = r.json().get("orders") or []
        found = next((o for o in orders if o["sto_id"] == sto_id), None)
        assert found is not None, f"Seeded {sto_id} not returned"
        assert "receipt_relocation" in found
        assert found["receipt_relocation"]["status"] == "done"
        assert found["receipt_relocation"]["to"] == "P8-RM"
    finally:
        db.stock_transfer_orders.delete_one({"_id": sto_id})


# ---- Regression: complete-manual-gi still works structurally ---------------
def test_complete_manual_gi_endpoint_reachable(api):
    """The endpoint itself must remain wired (returns 404 or 400 for a
    fake STO, not 405/500 from a signature mismatch)."""
    r = api.post(f"{BASE_URL}/api/stock-transfer/orders/STO-FAKE-181/complete-manual-gi")
    assert r.status_code in (400, 404), f"Got {r.status_code}: {r.text[:200]}"
