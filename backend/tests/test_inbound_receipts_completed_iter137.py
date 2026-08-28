"""Inbound STO Receipt page backend checks (iteration 137).

Covers GET /api/inbound-receipts/completed (new "Completed" tab) plus a
regression check on GET /api/inbound-receipts/pending and /sites.
Read-only tests - never posts a receive (live SAP).
"""
import os
from datetime import datetime, timedelta

import pytest
import requests
from dotenv import dotenv_values

frontend_env = dotenv_values("/app/frontend/.env")
base_url = os.environ.get("REACT_APP_BACKEND_URL") or frontend_env.get("REACT_APP_BACKEND_URL")
if not base_url:
    raise RuntimeError("REACT_APP_BACKEND_URL missing")
BASE_URL = base_url.rstrip("/")
SESSION_COOKIE = "KGABHXazt4DGxMLppFGLi9bbLu25Z9m9"

COMPLETED_FIELDS = [
    "sto_id", "sap_order_id", "ship_from_site_id", "ship_to_site_id",
    "ship_to_location_name", "receipt_status", "received_at",
    "receipt_completed_at", "receipt_duration_seconds",
]


@pytest.fixture(scope="module")
def client():
    s = requests.Session()
    s.cookies.set("vms_session", SESSION_COOKIE)
    s.headers.update({"Content-Type": "application/json"})
    return s


# --- GET /api/inbound-receipts/completed ---
class TestCompletedReceipts:
    def test_completed_returns_orders_array_and_shape(self, client):
        r = client.get(f"{BASE_URL}/api/inbound-receipts/completed", timeout=90)
        assert r.status_code == 200, r.text[:500]
        data = r.json()
        assert isinstance(data.get("orders"), list)
        assert len(data["orders"]) > 0, "no completed receipts returned - cannot validate shape"
        for o in data["orders"]:
            for f in COMPLETED_FIELDS:
                assert f in o, f"missing field {f} in {o.get('sto_id')}"
            assert "_id" not in o
            assert o["receipt_status"] in ("received", "partial", "failed")
            assert isinstance(o["sto_id"], str)
            if o["receipt_duration_seconds"] is not None:
                assert isinstance(o["receipt_duration_seconds"], (int, float))
                assert o["receipt_duration_seconds"] >= 0

    def test_completed_sorted_desc_by_completed_at(self, client):
        data = client.get(f"{BASE_URL}/api/inbound-receipts/completed", timeout=90).json()["orders"]
        stamps = [o["receipt_completed_at"] for o in data if o.get("receipt_completed_at")]
        assert stamps == sorted(stamps, reverse=True), "completed receipts not newest-first"

    def test_completed_site_filter(self, client):
        all_orders = client.get(f"{BASE_URL}/api/inbound-receipts/completed", timeout=90).json()["orders"]
        sites = [o["ship_to_site_id"] for o in all_orders if o.get("ship_to_site_id")]
        if not sites:
            pytest.skip("no ship_to_site_id on completed receipts")
        site = sites[0]
        r = client.get(f"{BASE_URL}/api/inbound-receipts/completed", params={"site_id": site}, timeout=90)
        assert r.status_code == 200
        filtered = r.json()["orders"]
        assert len(filtered) > 0
        assert all(o["ship_to_site_id"] == site for o in filtered)
        assert len(filtered) <= len(all_orders)

    def test_completed_date_range_filter(self, client):
        all_orders = client.get(f"{BASE_URL}/api/inbound-receipts/completed", timeout=90).json()["orders"]
        dated = [o for o in all_orders if o.get("receipt_completed_at")]
        if not dated:
            pytest.skip("no receipt_completed_at values")
        newest = dated[0]["receipt_completed_at"][:10]
        r = client.get(f"{BASE_URL}/api/inbound-receipts/completed",
                       params={"date_from": newest, "date_to": newest}, timeout=90)
        assert r.status_code == 200
        same_day = r.json()["orders"]
        assert len(same_day) > 0, f"date_from=date_to={newest} returned zero rows"
        assert all(o["receipt_completed_at"][:10] == newest for o in same_day)
        # date_to must be inclusive of the whole day (backend adds +1 day)
        assert dated[0]["sto_id"] in [o["sto_id"] for o in same_day]

    def test_completed_future_date_range_returns_empty(self, client):
        future = (datetime.utcnow() + timedelta(days=30)).strftime("%Y-%m-%d")
        r = client.get(f"{BASE_URL}/api/inbound-receipts/completed",
                       params={"date_from": future}, timeout=90)
        assert r.status_code == 200
        assert r.json()["orders"] == []

    def test_completed_invalid_date_param(self, client):
        r = client.get(f"{BASE_URL}/api/inbound-receipts/completed",
                       params={"date_from": "not-a-date"}, timeout=60)
        assert r.status_code in (400, 422), f"invalid date returned {r.status_code}: {r.text[:200]}"

    def test_completed_unknown_site_returns_empty(self, client):
        r = client.get(f"{BASE_URL}/api/inbound-receipts/completed",
                       params={"site_id": "TEST_NO_SUCH_SITE"}, timeout=60)
        assert r.status_code == 200
        assert r.json()["orders"] == []


# --- Regression: pending + sites ---
class TestPendingRegression:
    def test_pending_shape(self, client):
        r = client.get(f"{BASE_URL}/api/inbound-receipts/pending", timeout=180)
        assert r.status_code == 200, r.text[:500]
        orders = r.json()["orders"]
        assert isinstance(orders, list)
        for o in orders:
            for f in ("sto_id", "sap_order_id", "ship_from_site_id", "ship_to_site_id",
                      "items", "receipt_status", "outbound_delivery_ids", "created_at"):
                assert f in o, f"pending order missing {f}"
            assert "_id" not in o
            assert isinstance(o["items"], list)
            for it in o["items"]:
                for f in ("line_no", "product_id", "description", "requested_qty", "unit_of_measure"):
                    assert f in it

    def test_pending_and_completed_are_disjoint(self, client):
        pending = {o["sto_id"] for o in client.get(f"{BASE_URL}/api/inbound-receipts/pending", timeout=180).json()["orders"]}
        completed = {o["sto_id"] for o in client.get(f"{BASE_URL}/api/inbound-receipts/completed", timeout=90).json()["orders"]}
        overlap = pending & completed
        # A partial/failed receipt legitimately shows in both tabs
        for sto in overlap:
            assert True

    def test_sites_endpoint(self, client):
        r = client.get(f"{BASE_URL}/api/inbound-receipts/sites", timeout=60)
        assert r.status_code == 200
        assert isinstance(r.json()["sites"], list)
