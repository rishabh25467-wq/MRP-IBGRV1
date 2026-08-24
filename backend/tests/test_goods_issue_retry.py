"""Backend tests for the Goods Issue retry + live-stock-check feature (Aug 27 2026).

Covers:
  * GET  /api/stock-transfer/orders/{sto_id}          (new single-order lookup)
  * POST /api/stock-transfer/orders/{sto_id}/retry-goods-issue  (new manual retry)
  * stock_transfer_service.reset_goods_issue_for_retry (state-machine validation)
  * stock_transfer_service.try_post_goods_issue live-stock branch
    (insufficient_stock, deterministic via stub SAP clients)
  * stock_transfer_service._live_source_stock_qty (warehouse id + usable-stock filter)

Uses the existing synthetic Entra session helper tests/_setup_sto_session.py.
Live SAP: exactly one real retry is triggered, on the pre-existing real order
STO-000020 (already gi_status='failed' from a genuine SAP-side warehouse issue).
"""
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

import pytest
import requests
from dotenv import dotenv_values, load_dotenv

frontend_env = dotenv_values("/app/frontend/.env")
base_url = os.environ.get("REACT_APP_BACKEND_URL") or frontend_env.get("REACT_APP_BACKEND_URL")
if not base_url:
    raise RuntimeError("REACT_APP_BACKEND_URL missing")
BASE_URL = base_url.rstrip("/")
API = f"{BASE_URL}/api"

LIVE_FAILED_STO = "STO-000020"      # real order, gi_status='failed' (real SAP rejection)
POSTED_STO = "STO-000024"           # real order, gi_status='posted'
SAP_FAILED_STO = "STO-000021"       # order that never got created in SAP

SYNTH_STO = "TEST_STO_GI_RETRY"


@pytest.fixture(scope="module")
def session_token():
    out = subprocess.run(["python", "tests/_setup_sto_session.py"], cwd="/app/backend",
                         capture_output=True, text=True)
    token = out.stdout.strip().splitlines()[-1]
    assert token.startswith("TEST_sto_session_token"), out.stdout + out.stderr
    yield token
    subprocess.run(["python", "tests/_setup_sto_session.py", "--cleanup"], cwd="/app/backend",
                   capture_output=True, text=True)


@pytest.fixture(scope="module")
def client(session_token):
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json", "Cookie": f"vms_session={session_token}"})
    return s


@pytest.fixture(scope="module")
def mongo_db():
    sys.path.insert(0, "/app/backend")
    from pymongo import MongoClient
    load_dotenv("/app/backend/.env")
    return MongoClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]


# ---------------------------------------------------------------- GET single order
class TestGetSingleOrder:
    def test_get_existing_order(self, client):
        r = client.get(f"{API}/stock-transfer/orders/{LIVE_FAILED_STO}")
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["sto_id"] == LIVE_FAILED_STO
        assert d["status"] == "created_in_sap"
        assert "gi_status" in d and "gi_error" in d
        assert "_id" not in d, "MongoDB _id must not leak into the API response"
        assert isinstance(d.get("items"), list) and len(d["items"]) >= 1
        assert d["items"][0]["product_id"]

    def test_get_matches_list_endpoint(self, client):
        listed = client.get(f"{API}/stock-transfer/orders").json()
        one = client.get(f"{API}/stock-transfer/orders/{LIVE_FAILED_STO}").json()
        match = [o for o in listed if o["sto_id"] == LIVE_FAILED_STO]
        assert match, "order missing from list endpoint"
        for key in ("status", "gi_status", "sap_order_id"):
            assert match[0].get(key) == one.get(key)

    def test_get_unknown_order_404(self, client):
        r = client.get(f"{API}/stock-transfer/orders/STO-DOES-NOT-EXIST")
        assert r.status_code == 404, r.text

    def test_get_requires_auth(self):
        r = requests.get(f"{API}/stock-transfer/orders/{LIVE_FAILED_STO}")
        assert r.status_code in (401, 403), r.status_code

    def test_route_not_shadowed_by_sap_status(self, client):
        """/orders/sap-status/{job_id} must still resolve to its own handler."""
        r = client.get(f"{API}/stock-transfer/orders/sap-status/unknown-job-id")
        assert r.status_code == 404
        assert "job_id" in (r.json().get("detail") or "").lower()


# ---------------------------------------------------------------- retry validation
class TestRetryGoodsIssueValidation:
    def test_retry_requires_auth(self):
        r = requests.post(f"{API}/stock-transfer/orders/{LIVE_FAILED_STO}/retry-goods-issue")
        assert r.status_code in (401, 403), r.status_code

    def test_retry_unknown_order_rejected(self, client):
        r = client.post(f"{API}/stock-transfer/orders/STO-NOPE/retry-goods-issue")
        assert r.status_code in (400, 404), r.text
        assert "not found" in r.text.lower()

    def test_retry_order_not_created_in_sap_rejected(self, client):
        r = client.post(f"{API}/stock-transfer/orders/{SAP_FAILED_STO}/retry-goods-issue")
        assert r.status_code == 400, r.text
        assert "created in sap" in r.json()["detail"].lower()

    def test_retry_already_posted_rejected(self, client):
        r = client.post(f"{API}/stock-transfer/orders/{POSTED_STO}/retry-goods-issue")
        assert r.status_code == 400, r.text
        assert "failed" in r.json()["detail"].lower()
        # state untouched
        assert client.get(f"{API}/stock-transfer/orders/{POSTED_STO}").json()["gi_status"] == "posted"

    def test_retry_rejected_while_awaiting_delivery(self, client, mongo_db):
        """A retry must not be accepted for an order already awaiting delivery."""
        col = mongo_db["stock_transfer_orders"]
        mongo_db["stock_transfer_orders"].insert_one({
            "_id": SYNTH_STO, "sto_id": SYNTH_STO, "status": "created_in_sap",
            "gi_status": "awaiting_delivery", "gi_error": None,
            "ship_from_site_id": "P1", "ship_to_site_id": "P8",
            "sap_order_id": "TEST", "sap_order_uuid": "00000000-0000-0000-0000-000000000000",
            "items": [{"product_id": "P27175", "source_warehouse_id": "P1-RTV", "requested_qty": 1.0}],
            "created_at": datetime.now(timezone.utc),
        })
        try:
            r = client.post(f"{API}/stock-transfer/orders/{SYNTH_STO}/retry-goods-issue")
            assert r.status_code == 400, r.text
        finally:
            col.delete_one({"_id": SYNTH_STO})


# ---------------------------------------------------------------- live retry (real SAP)
class TestRetryGoodsIssueLive:
    def test_live_retry_resets_then_reattempts(self, client, mongo_db):
        before = client.get(f"{API}/stock-transfer/orders/{LIVE_FAILED_STO}").json()
        if before["gi_status"] not in ("failed", "not_found_timeout"):
            pytest.skip(f"{LIVE_FAILED_STO} gi_status is {before['gi_status']} - not retryable right now")

        r = client.post(f"{API}/stock-transfer/orders/{LIVE_FAILED_STO}/retry-goods-issue")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "restarted"

        # Immediate reset to awaiting_delivery + gi_error cleared
        mid = client.get(f"{API}/stock-transfer/orders/{LIVE_FAILED_STO}").json()
        assert mid["gi_status"] == "awaiting_delivery", mid
        assert not mid.get("gi_error")

        # Background loop live-checks stock then re-attempts within ~40s
        final = None
        deadline = time.time() + 75
        while time.time() < deadline:
            time.sleep(6)
            cur = client.get(f"{API}/stock-transfer/orders/{LIVE_FAILED_STO}").json()
            if cur["gi_status"] != "awaiting_delivery":
                final = cur
                break
        assert final is not None, "gi_status never moved off awaiting_delivery within 75s"
        assert final["gi_status"] in ("posted", "failed", "insufficient_stock"), final
        if final["gi_status"] in ("failed", "insufficient_stock"):
            assert final.get("gi_error"), "failed/insufficient_stock must carry gi_error text"


# ---------------------------------------------------------------- service-level unit tests
class _StubInventoryClient:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def get_inventory_detail(self, warehouse_ids=None, **kwargs):
        self.calls.append(warehouse_ids)
        return self.rows


class _StubDeliveryClient:
    def __init__(self, item):
        self.item = item
        self.posted = []

    def find_delivery_request_item(self, uuid):
        return self.item

    def post_goods_issue(self, object_id):
        self.posted.append(object_id)
        return {"ok": True}


@pytest.fixture(scope="module")
def svc():
    sys.path.insert(0, "/app/backend")
    import stock_transfer_service
    return stock_transfer_service


class TestLiveStockHelper:
    def test_builds_site_slash_warehouse_id_and_filters(self, svc):
        stub = _StubInventoryClient([
            {"product_id": "P27175", "qty": 100, "stock_status": "1", "restricted": False},
            {"product_id": "P27175", "qty": 5, "stock_status": "3", "restricted": True},
            {"product_id": "OTHER", "qty": 999, "stock_status": "1", "restricted": False},
        ])
        qty = svc._live_source_stock_qty(stub, "P1", "P1-RTV", "P27175")
        assert stub.calls == [["P1/P1-RTV"]]
        assert qty == 100  # restricted row and other product excluded


class TestInsufficientStockBranch:
    def _seed(self, db, requested_qty):
        db["stock_transfer_orders"].delete_one({"_id": SYNTH_STO})
        db["stock_transfer_orders"].insert_one({
            "_id": SYNTH_STO, "sto_id": SYNTH_STO, "status": "created_in_sap",
            "gi_status": "awaiting_delivery", "gi_error": None,
            "ship_from_site_id": "P1", "ship_to_site_id": "P8",
            "sap_order_id": "TEST", "sap_order_uuid": "00000000-0000-0000-0000-000000000000",
            "items": [{"product_id": "P27175", "source_warehouse_id": "P1-RTV",
                       "requested_qty": requested_qty}],
            "created_at": datetime.now(timezone.utc),
        })

    def test_short_stock_sets_insufficient_and_skips_sap_post(self, svc, mongo_db):
        self._seed(mongo_db, 500.0)
        inv = _StubInventoryClient([{"product_id": "P27175", "qty": 10, "stock_status": "1", "restricted": False}])
        deliv = _StubDeliveryClient({"object_id": "ODR-1", "order_fulfilment_status": "1"})
        try:
            outcome = svc.try_post_goods_issue(mongo_db, deliv, inv, SYNTH_STO)
            assert outcome == "waiting"
            assert deliv.posted == [], "SAP Goods Issue must NOT be called when live stock is short"
            doc = mongo_db["stock_transfer_orders"].find_one({"_id": SYNTH_STO})
            assert doc["gi_status"] == "insufficient_stock"
            err = doc["gi_error"]
            assert "Insufficient live stock in P1-RTV for P27175" in err
            assert "needed 500" in err and "available 10" in err
            assert doc["outbound_delivery_object_id"] == "ODR-1"
        finally:
            mongo_db["stock_transfer_orders"].delete_one({"_id": SYNTH_STO})

    def test_sufficient_stock_posts_goods_issue(self, svc, mongo_db):
        self._seed(mongo_db, 2.0)
        inv = _StubInventoryClient([{"product_id": "P27175", "qty": 50, "stock_status": "1", "restricted": False}])
        deliv = _StubDeliveryClient({"object_id": "ODR-2", "order_fulfilment_status": "1"})
        try:
            outcome = svc.try_post_goods_issue(mongo_db, deliv, inv, SYNTH_STO)
            assert outcome == "posted"
            assert deliv.posted == ["ODR-2"]
            doc = mongo_db["stock_transfer_orders"].find_one({"_id": SYNTH_STO})
            assert doc["gi_status"] == "posted" and doc["gi_error"] is None
        finally:
            mongo_db["stock_transfer_orders"].delete_one({"_id": SYNTH_STO})

    def test_no_delivery_yet_does_not_check_stock(self, svc, mongo_db):
        self._seed(mongo_db, 2.0)
        inv = _StubInventoryClient([])
        deliv = _StubDeliveryClient(None)
        try:
            assert svc.try_post_goods_issue(mongo_db, deliv, inv, SYNTH_STO) == "waiting"
            assert inv.calls == [], "should not hit live inventory before SAP produced the delivery"
            doc = mongo_db["stock_transfer_orders"].find_one({"_id": SYNTH_STO})
            assert doc["gi_status"] == "awaiting_delivery"
        finally:
            mongo_db["stock_transfer_orders"].delete_one({"_id": SYNTH_STO})


class TestResetForRetry:
    def test_reset_from_failed(self, svc, mongo_db):
        mongo_db["stock_transfer_orders"].delete_one({"_id": SYNTH_STO})
        mongo_db["stock_transfer_orders"].insert_one({
            "_id": SYNTH_STO, "sto_id": SYNTH_STO, "status": "created_in_sap",
            "gi_status": "failed", "gi_error": "old error",
            "ship_from_site_id": "P1", "items": [{"product_id": "P27175", "source_warehouse_id": "P1-RTV", "requested_qty": 1}],
            "created_at": datetime.now(timezone.utc),
        })
        try:
            svc.reset_goods_issue_for_retry(mongo_db, SYNTH_STO)
            doc = mongo_db["stock_transfer_orders"].find_one({"_id": SYNTH_STO})
            assert doc["gi_status"] == "awaiting_delivery" and doc["gi_error"] is None
        finally:
            mongo_db["stock_transfer_orders"].delete_one({"_id": SYNTH_STO})

    def test_reset_from_insufficient_stock_rejected(self, svc, mongo_db):
        """insufficient_stock is auto-retried by the loop; manual reset is refused."""
        mongo_db["stock_transfer_orders"].delete_one({"_id": SYNTH_STO})
        mongo_db["stock_transfer_orders"].insert_one({
            "_id": SYNTH_STO, "sto_id": SYNTH_STO, "status": "created_in_sap",
            "gi_status": "insufficient_stock", "gi_error": "short",
            "ship_from_site_id": "P1", "items": [{"product_id": "P27175", "source_warehouse_id": "P1-RTV", "requested_qty": 1}],
            "created_at": datetime.now(timezone.utc),
        })
        try:
            with pytest.raises(svc.StockTransferValidationError):
                svc.reset_goods_issue_for_retry(mongo_db, SYNTH_STO)
        finally:
            mongo_db["stock_transfer_orders"].delete_one({"_id": SYNTH_STO})
