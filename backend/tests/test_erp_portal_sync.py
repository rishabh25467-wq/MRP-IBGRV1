"""Backend tests for the NEW legacy-ERP-portal Delivery Challan sync
(Radish portal on MS SQL Server) fired after every live SAP Stock
Transfer Order creation (iteration 115).

Modules covered:
  - erp_portal_client.ERPPortalClient (connectivity / failover, NO writes)
  - stock_transfer_service.sync_to_erp_portal / mark_erp_portal_failed
    (field mapping verified against a fake client - no live writes)
  - GET /api/stock-transfer/orders + /orders/{id} exposing the new
    erp_portal_status / erp_portal_error / erp_sale_no / erp_sale_noc
"""
import os
import subprocess
import sys
from datetime import date, datetime

import pytest
import requests
from dotenv import dotenv_values, load_dotenv

sys.path.insert(0, "/app/backend")
load_dotenv("/app/backend/.env")

frontend_env = dotenv_values("/app/frontend/.env")
base_url = os.environ.get("REACT_APP_BACKEND_URL") or frontend_env.get("REACT_APP_BACKEND_URL")
if not base_url:
    raise RuntimeError("REACT_APP_BACKEND_URL missing")
BASE_URL = base_url.rstrip("/")
API = f"{BASE_URL}/api"


@pytest.fixture(scope="session")
def session_token():
    out = subprocess.run(["python", "tests/_setup_sto_session.py"], cwd="/app/backend",
                         capture_output=True, text=True)
    token = out.stdout.strip().splitlines()[-1]
    assert token.startswith("TEST_sto_session_token"), out.stdout + out.stderr
    return token


@pytest.fixture(scope="session")
def client(session_token):
    s = requests.Session()
    s.cookies.set("vms_session", session_token)
    return s


@pytest.fixture(scope="session")
def mongo_db():
    from pymongo import MongoClient
    c = MongoClient(os.environ["MONGO_URL"])
    return c[os.environ["DB_NAME"]]


# ---------------------------------------------------------------- client
class TestERPPortalClientConnectivity:
    """Live connectivity + failover, read-only (SELECT 1) - no inserts."""

    def test_env_vars_present(self):
        for key in ("ERP_MSSQL_PRIMARY_HOST", "ERP_MSSQL_PORT", "ERP_MSSQL_DATABASE",
                    "ERP_MSSQL_USERNAME", "ERP_MSSQL_PASSWORD"):
            assert os.environ.get(key), f"{key} missing from backend/.env"

    def test_live_connection_and_procs_exist(self):
        from erp_portal_client import ERPPortalClient
        c = ERPPortalClient(
            os.environ["ERP_MSSQL_PRIMARY_HOST"], os.environ.get("ERP_MSSQL_FALLBACK_HOST", ""),
            int(os.environ["ERP_MSSQL_PORT"]), os.environ["ERP_MSSQL_DATABASE"],
            os.environ["ERP_MSSQL_USERNAME"], os.environ["ERP_MSSQL_PASSWORD"],
        )
        conn = c._connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT name FROM sys.procedures WHERE name IN "
                        "('Pro_DeliveryChallan_Insert','Pro_DeliveryChallani_Insert')")
            names = sorted(r[0] for r in cur.fetchall())
            assert names == ["Pro_DeliveryChallani_Insert", "Pro_DeliveryChallan_Insert"] or \
                set(names) == {"Pro_DeliveryChallan_Insert", "Pro_DeliveryChallani_Insert"}, names
        finally:
            conn.close()

    def test_unreachable_host_raises_erp_portal_error(self):
        from erp_portal_client import ERPPortalClient, ERPPortalError
        c = ERPPortalClient("127.0.0.1", "", 14333, "x", "u", "p", timeout=3)
        with pytest.raises(ERPPortalError):
            c._connect()

    def test_failover_to_second_host(self):
        """Bad primary + good fallback must still connect."""
        from erp_portal_client import ERPPortalClient
        c = ERPPortalClient(
            "127.0.0.1", os.environ["ERP_MSSQL_PRIMARY_HOST"],
            int(os.environ["ERP_MSSQL_PORT"]), os.environ["ERP_MSSQL_DATABASE"],
            os.environ["ERP_MSSQL_USERNAME"], os.environ["ERP_MSSQL_PASSWORD"], timeout=10,
        )
        conn = c._connect()
        conn.close()


# ------------------------------------------------------------- mapping
class FakeERPClient:
    def __init__(self):
        self.calls = []

    def create_delivery_challan(self, header, items):
        self.calls.append((header, items))
        return {"sale_no": 99999, "sale_noc": 7}


class FakeValuationClient:
    def __init__(self, price):
        self.price = price
        self.asked = None

    def get_standard_costs(self, product_uuids):
        self.asked = list(product_uuids)
        return {u.upper(): {"amount": self.price, "currency": "INR"} for u in product_uuids}


def _seed_sto(db, sto_id, ship_from, ship_to, product_id="P27175", qty=3.0):
    db["component_master"].update_one(
        {"_id": product_id}, {"$setOnInsert": {"product_uuid": "aaaabbbb-1111-2222-3333-444455556666"}}, upsert=True)
    doc = {
        "_id": sto_id, "status": "created_in_sap", "ship_from_site_id": ship_from,
        "ship_to_site_id": ship_to, "ship_to_location_id": f"{ship_to}-RM",
        "source_warehouse_id": f"{ship_from}-RM",
        "requested_delivery_date": date.today().isoformat(),
        "vehicle_no": "TESTQA9001", "gr_no": "TESTQA-GR-1",
        "place_of_supply": "QA Place", "date_of_supply": date.today().isoformat(),
        "transportation_mode": "By Road",
        "items": [{"product_id": product_id, "description": "QA TEST PRODUCT",
                   "requested_qty": qty, "unit_of_measure": "EA"}],
        "created_at": datetime.utcnow(),
    }
    db["stock_transfer_orders"].replace_one({"_id": sto_id}, doc, upsert=True)
    return doc


class TestSyncFieldMapping:
    """Field mapping per user's spec, using a fake portal client."""

    @pytest.fixture(autouse=True)
    def _cleanup(self, mongo_db):
        yield
        mongo_db["stock_transfer_orders"].delete_many({"_id": {"$regex": "^TESTQA-ERP-"}})

    def test_comp_code_ri_for_p8(self, mongo_db):
        import stock_transfer_service as sts
        _seed_sto(mongo_db, "TESTQA-ERP-1", "P8", "P1")
        fake, val = FakeERPClient(), FakeValuationClient(2.46)
        sts.sync_to_erp_portal(mongo_db, fake, val, "TESTQA-ERP-1")
        header, items = fake.calls[0]
        assert header["comp_code"] == "RI"
        assert header["pcode"] == "P1"
        assert header["veh_no"] == "TESTQA9001"
        assert header["gr_no"] == "TESTQA-GR-1"
        assert header["marks"] == "QA Place"
        assert header["gr_date"] == date.today().isoformat()
        assert isinstance(header["sale_date"], datetime)
        # Rate = moving average x qty
        assert items[0]["rate"] == 2.46
        assert items[0]["qty"] == 3.0
        assert items[0]["amt"] == round(2.46 * 3.0, 3)
        assert items[0]["taxable_amt"] == items[0]["amt"]
        assert header["amount"] == round(2.46 * 3.0, 2)
        assert header["ttaxable_amt"] == round(2.46 * 3.0, 2)
        # intentionally blank fields
        assert not items[0]["hsn_no"]
        for k in ("elec_ref_no", "padd_code1", "padd_code2", "trans", "term1", "term2", "term3", "emp_no"):
            assert not header[k], k
        assert header["tdis_amt"] == 0
        # DB updated
        doc = mongo_db["stock_transfer_orders"].find_one({"_id": "TESTQA-ERP-1"})
        assert doc["erp_portal_status"] == "synced"
        assert doc["erp_sale_no"] == 99999 and doc["erp_sale_noc"] == 7
        assert doc["erp_portal_error"] is None

    def test_comp_code_ri_for_p1(self, mongo_db):
        import stock_transfer_service as sts
        _seed_sto(mongo_db, "TESTQA-ERP-2", "P1", "P9")
        fake = FakeERPClient()
        sts.sync_to_erp_portal(mongo_db, fake, FakeValuationClient(1.0), "TESTQA-ERP-2")
        assert fake.calls[0][0]["comp_code"] == "RI"

    @pytest.mark.parametrize("site", ["P2", "P9", "P3"])
    def test_comp_code_rt_for_other_sites(self, mongo_db, site):
        import stock_transfer_service as sts
        _seed_sto(mongo_db, f"TESTQA-ERP-3-{site}", site, "P8")
        fake = FakeERPClient()
        sts.sync_to_erp_portal(mongo_db, fake, FakeValuationClient(1.0), f"TESTQA-ERP-3-{site}")
        assert fake.calls[0][0]["comp_code"] == "RT"
        assert fake.calls[0][0]["pcode"] == "P8"

    def test_missing_price_falls_back_to_zero_rate(self, mongo_db):
        import stock_transfer_service as sts

        class NoPrice:
            def get_standard_costs(self, uuids):
                return {}

        _seed_sto(mongo_db, "TESTQA-ERP-4", "P8", "P1")
        fake = FakeERPClient()
        sts.sync_to_erp_portal(mongo_db, fake, NoPrice(), "TESTQA-ERP-4")
        assert fake.calls[0][1][0]["rate"] == 0.0
        assert fake.calls[0][0]["amount"] == 0

    def test_mark_erp_portal_failed(self, mongo_db):
        import stock_transfer_service as sts
        _seed_sto(mongo_db, "TESTQA-ERP-5", "P8", "P1")
        sts.mark_erp_portal_failed(mongo_db, "TESTQA-ERP-5", "boom")
        doc = mongo_db["stock_transfer_orders"].find_one({"_id": "TESTQA-ERP-5"})
        assert doc["erp_portal_status"] == "failed"
        assert doc["erp_portal_error"] == "boom"

    def test_unknown_sto_raises(self, mongo_db):
        import stock_transfer_service as sts
        with pytest.raises(sts.StockTransferOrderNotFoundError):
            sts.sync_to_erp_portal(mongo_db, FakeERPClient(), FakeValuationClient(1.0), "NOPE-123")


# ------------------------------------------------------------ API shape
class TestOrderApiExposesErpFields:
    def test_list_orders_includes_erp_fields(self, client):
        r = client.get(f"{API}/stock-transfer/orders")
        assert r.status_code == 200, r.text
        body = r.json()
        orders = body if isinstance(body, list) else body.get("orders", [])
        assert isinstance(orders, list) and orders, "no existing orders to inspect"
        synced = [o for o in orders if o.get("erp_portal_status") == "synced"]
        assert synced, f"expected at least one synced order; statuses={[o.get('erp_portal_status') for o in orders[:10]]}"
        o = synced[0]
        assert isinstance(o["erp_sale_no"], int) and o["erp_sale_no"] > 0
        assert isinstance(o["erp_sale_noc"], int)
        assert "_id" not in o

    def test_get_single_order_includes_erp_fields(self, client):
        r = client.get(f"{API}/stock-transfer/orders")
        body = r.json()
        orders = body if isinstance(body, list) else body.get("orders", [])
        synced = [o for o in orders if o.get("erp_portal_status") == "synced"]
        if not synced:
            pytest.fail("no synced order available for detail check")
        sto_id = synced[0]["sto_id"]
        d = client.get(f"{API}/stock-transfer/orders/{sto_id}")
        assert d.status_code == 200, d.text
        data = d.json()
        assert data["erp_portal_status"] == "synced"
        assert data["erp_portal_error"] in (None, "")
        assert isinstance(data["erp_sale_no"], int)
        assert isinstance(data["erp_sale_noc"], int)
        assert "_id" not in data

    def test_orders_requires_auth(self):
        r = requests.get(f"{API}/stock-transfer/orders")
        assert r.status_code in (401, 403), r.status_code
