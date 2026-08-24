"""Backend tests for the Inter-Plant Stock Transfer (STO) feature (Aug 2026).

Covers: /api/stock-transfer/inventory, ship-to-sites, locations, suggest-source,
POST+GET /orders (validation matrix), parse-nl (LLM), and auth gating.

Requires a synthetic Entra session - created/cleaned by fixtures via
tests/_setup_sto_session.py (cookie name `vms_session`).
"""
import os
import subprocess

import pytest
import requests
from dotenv import dotenv_values
from datetime import date, timedelta

frontend_env = dotenv_values("/app/frontend/.env")
base_url = os.environ.get("REACT_APP_BACKEND_URL") or frontend_env.get("REACT_APP_BACKEND_URL")
if not base_url:
    raise RuntimeError("REACT_APP_BACKEND_URL missing")
BASE_URL = base_url.rstrip("/")
API = f"{BASE_URL}/api"

# Real products verified present in inventory_cache (single-site each)
P9_PRODUCT = "100002693-C1"   # P9 only, P9-SFG, qty 42408 (company RT)
P2_PRODUCT = "09200597-01"    # P2 only, P2-RM, qty 400 (company RT)
P1_PRODUCT = "1124-274"       # P1 only, P1-SFG, qty 8791 (company RI)

TOMORROW = (date.today() + timedelta(days=1)).isoformat()
YESTERDAY = (date.today() - timedelta(days=1)).isoformat()


@pytest.fixture(scope="session")
def session_token():
    out = subprocess.run(["python", "tests/_setup_sto_session.py"], cwd="/app/backend",
                         capture_output=True, text=True)
    token = out.stdout.strip().splitlines()[-1]
    assert token.startswith("TEST_sto_session_token"), out.stdout + out.stderr
    yield token
    subprocess.run(["python", "tests/_setup_sto_session.py", "--cleanup"], cwd="/app/backend",
                   capture_output=True, text=True)


@pytest.fixture(scope="session")
def client(session_token):
    s = requests.Session()
    s.cookies.set("vms_session", session_token)
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="session")
def created_stos():
    ids = []
    yield ids
    import sys
    sys.path.insert(0, "/app/backend")
    from pymongo import MongoClient
    from dotenv import load_dotenv
    load_dotenv("/app/backend/.env")
    db = MongoClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]
    for sto_id in ids:
        db["stock_transfer_orders"].delete_one({"_id": sto_id})


# ---------------- auth gating ----------------
class TestAuthGating:
    def test_inventory_requires_session(self):
        r = requests.get(f"{API}/stock-transfer/inventory", params={"product_id": P9_PRODUCT})
        assert r.status_code == 401, r.text

    def test_orders_list_requires_session(self):
        r = requests.get(f"{API}/stock-transfer/orders")
        assert r.status_code == 401, r.text

    def test_inventory_page_permission_allows(self, client):
        r = client.get(f"{API}/stock-transfer/orders")
        assert r.status_code == 200, r.text

    def test_product_search_allowed_for_inventory_page(self, client):
        r = client.get(f"{API}/products/search", params={"q": "1124", "limit": 5})
        assert r.status_code == 200, r.text
        assert isinstance(r.json(), list)


# ---------------- inventory lookup ----------------
class TestInventoryLookup:
    def test_known_product_locations(self, client):
        r = client.get(f"{API}/stock-transfer/inventory", params={"product_id": P9_PRODUCT})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["product_id"] == P9_PRODUCT
        assert len(d["locations"]) >= 1
        for loc in d["locations"]:
            assert loc["qty"] > 0
            assert loc["site_id"]
            assert loc["warehouse_id"]
            assert "/" not in loc["warehouse_id"]
        # sorted desc by qty
        qtys = [l["qty"] for l in d["locations"]]
        assert qtys == sorted(qtys, reverse=True)
        assert {l["site_id"] for l in d["locations"]} == {"P9"}

    def test_unknown_product_returns_empty(self, client):
        r = client.get(f"{API}/stock-transfer/inventory", params={"product_id": "TEST_NOPE_XYZ"})
        assert r.status_code == 200, r.text
        assert r.json()["locations"] == []


# ---------------- ship-to sites (company filter) ----------------
class TestShipToSites:
    def test_rt_company_site(self, client):
        r = client.get(f"{API}/stock-transfer/ship-to-sites", params={"ship_from_site_id": "P9"})
        assert r.status_code == 200, r.text
        sites = r.json()["sites"]
        assert "P9" not in sites
        assert "P1" not in sites and "P8" not in sites
        assert "P2" in sites

    def test_ri_company_site(self, client):
        r = client.get(f"{API}/stock-transfer/ship-to-sites", params={"ship_from_site_id": "P1"})
        assert r.status_code == 200, r.text
        sites = r.json()["sites"]
        assert sites == ["P8"] or ("P8" in sites and "P1" not in sites and "P9" not in sites)


# ---------------- locations ----------------
class TestLocations:
    def test_locations_differ_per_site(self, client):
        p9 = client.get(f"{API}/stock-transfer/locations", params={"site_id": "P9"}).json()["warehouses"]
        p2 = client.get(f"{API}/stock-transfer/locations", params={"site_id": "P2"}).json()["warehouses"]
        assert len(p9) > 0 and len(p2) > 0
        assert [w["warehouse_id"] for w in p9] != [w["warehouse_id"] for w in p2]
        for w in p9:
            assert w["warehouse_id"].startswith("P9")

    def test_unknown_site_empty(self, client):
        r = client.get(f"{API}/stock-transfer/locations", params={"site_id": "ZZZ"})
        assert r.status_code == 200
        assert r.json()["warehouses"] == []


# ---------------- suggest source ----------------
class TestSuggestSource:
    def test_suggests_highest_qty(self, client):
        inv = client.get(f"{API}/stock-transfer/inventory", params={"product_id": P9_PRODUCT}).json()
        r = client.get(f"{API}/stock-transfer/suggest-source", params={"product_id": P9_PRODUCT})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["suggested"]["warehouse_id"] == inv["locations"][0]["warehouse_id"]
        assert d["suggested"]["qty"] == inv["locations"][0]["qty"]
        assert "Highest available stock" in d["reason"]

    def test_excludes_ship_to_site(self, client):
        r = client.get(f"{API}/stock-transfer/suggest-source",
                       params={"product_id": P9_PRODUCT, "ship_to_site_id": "P9"})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["suggested"] is None
        assert "other site" in d["reason"]

    def test_no_stock_product(self, client):
        r = client.get(f"{API}/stock-transfer/suggest-source", params={"product_id": "TEST_NOPE_XYZ"})
        assert r.status_code == 200
        assert r.json()["suggested"] is None


# ---------------- order creation + validation ----------------
class TestOrderCreation:
    def _payload(self, **over):
        p = {
            "ship_to_site_id": "P2",
            "ship_to_location_id": "P2-RM",
            "requested_delivery_date": TOMORROW,
            # GST/e-way fields became mandatory (Pydantic-required) in a later
            # iteration - test payload updated accordingly (iteration 115).
            "transportation_mode": "By Road",
            "vehicle_no": "TESTQA1150",
            "place_of_supply": "TESTQA Place",
            "gr_no": "TESTQA-GR-115",
            "date_of_supply": TOMORROW,
            "items": [{"product_id": P9_PRODUCT, "source_warehouse_id": "P9-SFG", "requested_qty": 5}],
        }
        p.update(over)
        return p

    def test_create_success_and_persist(self, client, created_stos):
        r = client.post(f"{API}/stock-transfer/orders", json=self._payload())
        assert r.status_code == 200, r.text
        d = r.json()
        created_stos.append(d["sto_id"])
        assert d["sto_id"].startswith("STO-") and len(d["sto_id"]) == 10
        assert d["status"] == "pending_sap"
        assert d["delivery_priority"] == "Immediate"
        assert d["ship_from_site_id"] == "P9"
        assert d["ship_to_site_id"] == "P2"
        assert d["ship_to_location_id"] == "P2-RM"
        assert d["requested_delivery_date"] == TOMORROW
        assert "_id" not in d
        assert d["items"][0]["product_id"] == P9_PRODUCT
        assert d["items"][0]["requested_qty"] == 5
        assert d["items"][0]["available_qty"] > 5
        assert d["created_by"] == "QA STO"

        # verify persisted in listing
        lst = client.get(f"{API}/stock-transfer/orders").json()
        assert lst[0]["sto_id"] == d["sto_id"]
        assert all("_id" not in o for o in lst)

    def test_insufficient_stock_blocked(self, client):
        r = client.post(f"{API}/stock-transfer/orders", json=self._payload(
            items=[{"product_id": P9_PRODUCT, "source_warehouse_id": "P9-SFG", "requested_qty": 99999999}]))
        assert r.status_code == 400, r.text
        assert "Insufficient Stock" in r.json()["detail"]

    def test_zero_qty_blocked(self, client):
        r = client.post(f"{API}/stock-transfer/orders", json=self._payload(
            items=[{"product_id": P9_PRODUCT, "source_warehouse_id": "P9-SFG", "requested_qty": 0}]))
        assert r.status_code == 400, r.text
        assert "greater than zero" in r.json()["detail"]

    def test_negative_qty_blocked(self, client):
        r = client.post(f"{API}/stock-transfer/orders", json=self._payload(
            items=[{"product_id": P9_PRODUCT, "source_warehouse_id": "P9-SFG", "requested_qty": -5}]))
        assert r.status_code == 400, r.text

    def test_past_date_blocked(self, client):
        r = client.post(f"{API}/stock-transfer/orders", json=self._payload(requested_delivery_date=YESTERDAY))
        assert r.status_code == 400, r.text
        assert "earlier than today" in r.json()["detail"]

    def test_missing_date_blocked(self, client):
        r = client.post(f"{API}/stock-transfer/orders", json=self._payload(requested_delivery_date=""))
        assert r.status_code == 400, r.text
        assert "Requested Delivery Date is required" in r.json()["detail"]

    def test_no_items_blocked(self, client):
        r = client.post(f"{API}/stock-transfer/orders", json=self._payload(items=[]))
        assert r.status_code == 400, r.text
        assert "At least one item" in r.json()["detail"]

    def test_cross_company_ship_to_blocked(self, client):
        # ship-from P9 (RT), ship-to P1 (RI)
        r = client.post(f"{API}/stock-transfer/orders", json=self._payload(
            ship_to_site_id="P1", ship_to_location_id="P1-SFG"))
        assert r.status_code == 400, r.text
        assert "same Company" in r.json()["detail"]

    def test_same_site_ship_to_blocked(self, client):
        locs = client.get(f"{API}/stock-transfer/locations", params={"site_id": "P9"}).json()["warehouses"]
        r = client.post(f"{API}/stock-transfer/orders", json=self._payload(
            ship_to_site_id="P9", ship_to_location_id=locs[0]["warehouse_id"]))
        assert r.status_code == 400, r.text
        assert "same as Ship-from" in r.json()["detail"]

    def test_unknown_ship_to_location_blocked(self, client):
        r = client.post(f"{API}/stock-transfer/orders", json=self._payload(ship_to_location_id="P2-FAKE"))
        assert r.status_code == 400, r.text
        assert "not a known warehouse" in r.json()["detail"]

    def test_cross_site_multi_item_blocked(self, client):
        r = client.post(f"{API}/stock-transfer/orders", json=self._payload(
            ship_to_site_id="P3",
            ship_to_location_id=client.get(f"{API}/stock-transfer/locations",
                                           params={"site_id": "P3"}).json()["warehouses"][0]["warehouse_id"],
            items=[
                {"product_id": P9_PRODUCT, "source_warehouse_id": "P9-SFG", "requested_qty": 1},
                {"product_id": P2_PRODUCT, "source_warehouse_id": "P2-RM", "requested_qty": 1},
            ]))
        assert r.status_code == 400, r.text
        assert "only have one Ship-from Site" in r.json()["detail"]

    def test_warehouse_without_stock_blocked(self, client):
        r = client.post(f"{API}/stock-transfer/orders", json=self._payload(
            items=[{"product_id": P9_PRODUCT, "source_warehouse_id": "P2-RM", "requested_qty": 1}]))
        assert r.status_code == 400, r.text
        assert "No usable stock" in r.json()["detail"]

    def test_missing_source_warehouse_blocked(self, client):
        r = client.post(f"{API}/stock-transfer/orders", json=self._payload(
            items=[{"product_id": P9_PRODUCT, "source_warehouse_id": "", "requested_qty": 1}]))
        assert r.status_code == 400, r.text
        assert "Source Warehouse is required" in r.json()["detail"]

    def test_sto_id_increments(self, client, created_stos):
        a = client.post(f"{API}/stock-transfer/orders", json=self._payload())
        b = client.post(f"{API}/stock-transfer/orders", json=self._payload())
        assert a.status_code == 200 and b.status_code == 200
        created_stos.extend([a.json()["sto_id"], b.json()["sto_id"]])
        assert int(b.json()["sto_id"][4:]) == int(a.json()["sto_id"][4:]) + 1


# ---------------- NL parse (LLM) ----------------
class TestNaturalLanguageParse:
    def test_parse_full_sentence(self, client):
        r = client.post(f"{API}/stock-transfer/parse-nl",
                        json={"text": f"transfer 250 of {P9_PRODUCT} to P2 by tomorrow"}, timeout=90)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["product_id"] == P9_PRODUCT
        assert float(d["quantity"]) == 250
        assert d["ship_to_site_id"] == "P2"
        assert d["requested_delivery_date"] == TOMORROW
        assert d["model_used"]

    def test_parse_partial_leaves_nulls(self, client):
        r = client.post(f"{API}/stock-transfer/parse-nl",
                        json={"text": f"move 10 {P9_PRODUCT}"}, timeout=90)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["ship_to_site_id"] is None
        assert d["requested_delivery_date"] is None
