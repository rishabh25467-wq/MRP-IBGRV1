"""iteration_132 - Outbound Goods Issue combine fix (multi-line STO -> ONE
outbound delivery via Playwright SAP UI automation).

FAST tests only (no live SAP writes) - read endpoints, validation, and
historical regression assertions on the main agent's own live test orders
(STO-000062..66). Live creation tests live in
test_sto_outbound_gi_live_iter132.py (marked, run separately).
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
SESSION_TOKEN = "TEST_sto_session_token_solo"


@pytest.fixture(scope="module")
def client():
    s = requests.Session()
    s.cookies.set("vms_session", SESSION_TOKEN)
    s.headers.update({"Content-Type": "application/json"})
    return s


# --- auth guard -------------------------------------------------------------
class TestAuth:
    def test_stock_transfer_orders_requires_session(self):
        r = requests.get(f"{BASE_URL}/api/stock-transfer/orders", timeout=60)
        assert r.status_code in (401, 403), r.status_code


# --- read endpoints backing the STO page ------------------------------------
class TestReadEndpoints:
    def test_list_orders(self, client):
        r = client.get(f"{BASE_URL}/api/stock-transfer/orders", timeout=120)
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list) and len(data) > 0
        first = data[0]
        for key in ("sto_id", "items", "gi_status", "ship_from_site_id", "ship_to_site_id"):
            assert key in first, f"missing {key} in list response"
        assert "_id" not in first, "raw mongo _id leaked in response"

    def test_order_detail(self, client):
        listing = client.get(f"{BASE_URL}/api/stock-transfer/orders", timeout=120).json()
        sto_id = listing[0]["sto_id"]
        r = client.get(f"{BASE_URL}/api/stock-transfer/orders/{sto_id}", timeout=60)
        assert r.status_code == 200
        d = r.json()
        assert d["sto_id"] == sto_id
        assert "_id" not in d

    def test_order_detail_unknown_id(self, client):
        r = client.get(f"{BASE_URL}/api/stock-transfer/orders/STO-NOPE-999", timeout=60)
        assert r.status_code == 404, r.status_code

    def test_inventory_lookup(self, client):
        r = client.get(f"{BASE_URL}/api/stock-transfer/inventory?product_id=G12LW", timeout=120)
        assert r.status_code == 200
        d = r.json()
        assert d["product_id"] == "G12LW"
        assert d["unit_of_measure"] == "EA"
        assert any(loc["warehouse_id"] == "P8-RM" and loc["qty"] > 0 for loc in d["locations"])

    def test_ship_to_sites_and_locations(self, client):
        r = client.get(f"{BASE_URL}/api/stock-transfer/ship-to-sites?ship_from_site_id=P8", timeout=60)
        assert r.status_code == 200
        assert isinstance(r.json()["sites"], list)
        r2 = client.get(f"{BASE_URL}/api/stock-transfer/locations?site_id=P1", timeout=60)
        assert r2.status_code == 200
        whs = r2.json()["warehouses"]
        assert any((w.get("warehouse_id") if isinstance(w, dict) else w) == "P1-RM" for w in whs), whs

    def test_suggest_source(self, client):
        r = client.get(f"{BASE_URL}/api/stock-transfer/suggest-source?product_id=G12LW&ship_to_site_id=P1", timeout=120)
        assert r.status_code == 200
        assert isinstance(r.json(), dict)


# --- create-payload validation (no SAP write happens on a 400) --------------
BASE_PAYLOAD = {
    "ship_to_site_id": "P1",
    "ship_to_location_id": "P1-RM",
    "requested_delivery_date": "2026-08-30",
    "transportation_mode": "By Road",
    "vehicle_no": "UP81AA0099",
    "place_of_supply": "Aligarh",
    "gr_no": "9001",
    "date_of_supply": "2026-08-30",
    "freight_forwarder": "Pooja Transport",
    "items": [{"product_id": "G12LW", "source_warehouse_id": "P8-RM", "requested_qty": 1}],
}


class TestCreateValidation:
    def test_missing_required_field_422(self, client):
        payload = {k: v for k, v in BASE_PAYLOAD.items() if k != "vehicle_no"}
        r = client.post(f"{BASE_URL}/api/stock-transfer/orders", json=payload, timeout=60)
        assert r.status_code == 422, r.text[:300]

    def test_empty_items_rejected(self, client):
        payload = dict(BASE_PAYLOAD, items=[])
        r = client.post(f"{BASE_URL}/api/stock-transfer/orders", json=payload, timeout=60)
        assert r.status_code in (400, 422), f"{r.status_code} {r.text[:300]}"

    def test_unknown_product_rejected(self, client):
        payload = dict(BASE_PAYLOAD, items=[
            {"product_id": "ZZZNOTREAL999", "source_warehouse_id": "P8-RM", "requested_qty": 1}])
        r = client.post(f"{BASE_URL}/api/stock-transfer/orders", json=payload, timeout=120)
        assert r.status_code == 400, f"{r.status_code} {r.text[:300]}"

    def test_qty_over_available_rejected(self, client):
        payload = dict(BASE_PAYLOAD, items=[
            {"product_id": "G12LW", "source_warehouse_id": "P8-RM", "requested_qty": 99999999}])
        r = client.post(f"{BASE_URL}/api/stock-transfer/orders", json=payload, timeout=120)
        assert r.status_code == 400, f"{r.status_code} {r.text[:300]}"


# --- historical regression: the fix's own live-verified orders -------------
class TestCombinedDeliveryRegression:
    def test_post_fix_multiline_orders_have_exactly_one_delivery_id(self, client):
        orders = {o["sto_id"]: o for o in client.get(f"{BASE_URL}/api/stock-transfer/orders", timeout=120).json()}
        for sto_id in ("STO-000062", "STO-000063", "STO-000064", "STO-000065", "STO-000066"):
            o = orders.get(sto_id)
            if not o:
                pytest.skip(f"{sto_id} not present in DB")
            assert len(o["items"]) > 1, f"{sto_id} expected multi-line"
            assert o["gi_status"] == "posted", f"{sto_id} gi_status={o['gi_status']}"
            assert len(o.get("outbound_delivery_ids") or []) == 1, \
                f"{sto_id} has {o.get('outbound_delivery_ids')} (expected exactly 1 combined delivery)"

    def test_single_line_orders_still_have_one_delivery_id(self, client):
        orders = client.get(f"{BASE_URL}/api/stock-transfer/orders", timeout=120).json()
        singles = [o for o in orders if len(o.get("items") or []) == 1 and o.get("gi_status") == "posted"]
        assert singles, "no posted single-line STO in history to regress against"
        for o in singles:
            assert len(o.get("outbound_delivery_ids") or []) == 1, \
                f"{o['sto_id']} single-line has {o.get('outbound_delivery_ids')}"
