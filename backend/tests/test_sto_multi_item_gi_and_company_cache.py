"""Aug 27 2026 bug-fix verification (iteration 127):
1. Multi-item Goods Issue (stock_transfer_service.try_post_goods_issue +
   sap_outbound_delivery_client.find_delivery_request_items) - FULLY MOCKED,
   never touches the live SAP tenant.
2. Missing GSTIN/PAN for site "P2W" (company_cache_service pcode fallback) -
   real read-only Mongo query.
3. Regression on GET /api/stock-transfer/orders and .../delivery-note.
"""
import os
import sys
import uuid

import pytest
import requests
from dotenv import dotenv_values
from pymongo import MongoClient

sys.path.insert(0, "/app/backend")

import company_cache_service  # noqa: E402
import stock_transfer_service  # noqa: E402
from sap_outbound_delivery_client import SAPOutboundDeliveryError  # noqa: E402

backend_env = dotenv_values("/app/backend/.env")
frontend_env = dotenv_values("/app/frontend/.env")
BASE_URL = (os.environ.get("REACT_APP_BACKEND_URL") or frontend_env.get("REACT_APP_BACKEND_URL")).rstrip("/")
MONGO_URL = backend_env.get("MONGO_URL")
DB_NAME = backend_env.get("DB_NAME")
if not MONGO_URL or not DB_NAME:
    raise RuntimeError("MONGO_URL/DB_NAME missing from /app/backend/.env")

STO_COLLECTION = stock_transfer_service.STO_COLLECTION


@pytest.fixture(scope="module")
def db():
    client = MongoClient(MONGO_URL)
    yield client[DB_NAME]
    client.close()


# ---------------------------------------------------------------- mocks
class MockOutboundClient:
    """Fakes ONLY the HTTP layer's return values - no network calls."""

    def __init__(self, responses):
        # responses: list of "delivery items" lists, one per poll tick
        self._responses = list(responses)
        self.find_calls = []
        self.gi_calls = []
        self.gi_error = None

    def find_delivery_request_items(self, uuid_):
        self.find_calls.append(uuid_)
        idx = min(len(self.find_calls) - 1, len(self._responses) - 1)
        return self._responses[idx]

    def post_goods_issue(self, object_id):
        self.gi_calls.append(object_id)
        if self.gi_error:
            raise SAPOutboundDeliveryError(self.gi_error)
        return {"ok": True}


class MockInventoryClient:
    """stock_by_product: {product_id: qty available at its warehouse}"""

    def __init__(self, stock_by_product, default=0.0):
        self.stock_by_product = stock_by_product
        self.default = default
        self.calls = []

    def get_inventory_detail(self, warehouse_ids=None):
        self.calls.append(warehouse_ids)
        rows = []
        for pid, qty in self.stock_by_product.items():
            rows.append({"product_id": pid, "qty": qty, "stock_status": "1", "restricted": False})
        return rows


def _delivery_item(object_id, product_id, status="1"):
    return {
        "object_id": object_id,
        "order_fulfilment_status": status,
        "product_id": product_id,
        "description": f"desc {product_id}",
    }


@pytest.fixture
def sto_factory(db):
    created = []

    def _make(items, gi_status="awaiting_delivery", ship_from="P2W"):
        sto_id = f"TEST_STO_{uuid.uuid4().hex[:8]}"
        db[STO_COLLECTION].insert_one({
            "_id": sto_id,
            "status": "created_in_sap",
            "gi_status": gi_status,
            "ship_from_site_id": ship_from,
            "ship_to_site_id": "P1",
            "sap_order_id": "TEST30280",
            "sap_order_uuid": str(uuid.uuid4()).upper(),
            "items": items,
        })
        created.append(sto_id)
        return sto_id

    yield _make
    for sid in created:
        db[STO_COLLECTION].delete_one({"_id": sid})


def _line(pid, qty=10.0, wh="RM01"):
    return {"product_id": pid, "requested_qty": qty, "source_warehouse_id": wh, "description": pid}


# --------------------------------------------- ROOT CAUSE 1 (multi item)
class TestMultiItemGoodsIssue:
    """try_post_goods_issue must post GI once per delivery line."""

    def test_four_lines_all_sufficient_posts_four_times(self, db, sto_factory):
        products = ["HRPIPE3329", "MSPIPE402015", "MSPIPE408MM", "FLAT-BK21"]
        sto_id = sto_factory([_line(p) for p in products])
        items = [_delivery_item(f"OID{i}", p) for i, p in enumerate(products)]
        out = MockOutboundClient([items])
        inv = MockInventoryClient({p: 100.0 for p in products})

        result = stock_transfer_service.try_post_goods_issue(db, out, inv, sto_id)

        assert result == "posted", f"expected posted, got {result}"
        assert len(out.gi_calls) == 4, f"post_goods_issue called {len(out.gi_calls)} times, expected 4"
        assert sorted(out.gi_calls) == sorted([f"OID{i}" for i in range(4)])

        doc = db[STO_COLLECTION].find_one({"_id": sto_id})
        assert doc["gi_status"] == "posted"
        assert doc["gi_error"] is None
        assert doc["gi_job_running"] is False
        assert doc["outbound_delivery_object_ids"] == [f"OID{i}" for i in range(4)]
        assert doc["outbound_delivery_object_id"] == "OID0"

    def test_three_ready_one_short_then_next_tick_completes(self, db, sto_factory):
        products = ["HRPIPE3329", "MSPIPE402015", "MSPIPE408MM", "FLAT-BK21"]
        short = "FLAT-BK21"
        sto_id = sto_factory([_line(p, qty=10.0, wh="RM01") for p in products])
        tick1 = [_delivery_item(f"OID{i}", p) for i, p in enumerate(products)]
        # tick 2: the 3 posted lines are now Finished ("3"), the short one isn't
        tick2 = [
            _delivery_item(f"OID{i}", p, status=("1" if p == short else "3"))
            for i, p in enumerate(products)
        ]
        out = MockOutboundClient([tick1, tick2])
        stock = {p: 100.0 for p in products}
        stock[short] = 2.0
        inv = MockInventoryClient(stock)

        # ---- tick 1
        result = stock_transfer_service.try_post_goods_issue(db, out, inv, sto_id)
        assert result == "waiting", f"expected waiting, got {result}"
        assert len(out.gi_calls) == 3, f"expected 3 GI posts, got {out.gi_calls}"
        assert "OID3" not in out.gi_calls, "GI must NOT be posted for the insufficient-stock line"

        doc = db[STO_COLLECTION].find_one({"_id": sto_id})
        assert doc["gi_status"] == "insufficient_stock"
        assert short in doc["gi_error"], f"gi_error must name the short product: {doc['gi_error']}"
        assert "RM01" in doc["gi_error"], f"gi_error must name the warehouse: {doc['gi_error']}"

        # ---- tick 2 (stock replenished for the 4th line)
        inv.stock_by_product[short] = 500.0
        result2 = stock_transfer_service.try_post_goods_issue(db, out, inv, sto_id)
        assert result2 == "posted", f"expected posted on tick 2, got {result2}"
        assert len(out.gi_calls) == 4, f"already-finished lines must not be re-posted: {out.gi_calls}"
        assert out.gi_calls.count("OID0") == 1
        assert out.gi_calls[-1] == "OID3"

        doc = db[STO_COLLECTION].find_one({"_id": sto_id})
        assert doc["gi_status"] == "posted"
        assert doc["gi_error"] is None

    def test_all_finished_marks_posted_without_any_gi_call(self, db, sto_factory):
        products = ["A1", "A2"]
        sto_id = sto_factory([_line(p) for p in products])
        items = [_delivery_item(f"F{i}", p, status="3") for i, p in enumerate(products)]
        out = MockOutboundClient([items])
        inv = MockInventoryClient({p: 100.0 for p in products})
        assert stock_transfer_service.try_post_goods_issue(db, out, inv, sto_id) == "posted"
        assert out.gi_calls == [], "no GI should be posted when every line is already Finished"

    def test_single_item_regression(self, db, sto_factory):
        sto_id = sto_factory([_line("HRPIPE3329", qty=5.0)])
        out = MockOutboundClient([[_delivery_item("SOLO1", "HRPIPE3329")]])
        inv = MockInventoryClient({"HRPIPE3329": 50.0})
        assert stock_transfer_service.try_post_goods_issue(db, out, inv, sto_id) == "posted"
        assert out.gi_calls == ["SOLO1"]
        doc = db[STO_COLLECTION].find_one({"_id": sto_id})
        assert doc["gi_status"] == "posted"
        assert doc["outbound_delivery_object_id"] == "SOLO1"
        assert doc["outbound_delivery_object_ids"] == ["SOLO1"]

    def test_no_delivery_items_yet_returns_waiting(self, db, sto_factory):
        sto_id = sto_factory([_line("X1")])
        out = MockOutboundClient([[]])
        inv = MockInventoryClient({"X1": 100.0})
        assert stock_transfer_service.try_post_goods_issue(db, out, inv, sto_id) == "waiting"
        assert out.gi_calls == []
        assert inv.calls == [], "must not live-check stock before SAP produced the delivery"

    def test_already_posted_sto_short_circuits(self, db, sto_factory):
        sto_id = sto_factory([_line("X1")], gi_status="posted")
        out = MockOutboundClient([[_delivery_item("Z1", "X1")]])
        inv = MockInventoryClient({"X1": 100.0})
        assert stock_transfer_service.try_post_goods_issue(db, out, inv, sto_id) == "posted"
        assert out.find_calls == [] and out.gi_calls == []

    def test_sap_rejection_marks_failed_and_raises(self, db, sto_factory):
        products = ["B1", "B2"]
        sto_id = sto_factory([_line(p) for p in products])
        out = MockOutboundClient([[_delivery_item(f"E{i}", p) for i, p in enumerate(products)]])
        out.gi_error = "HTTP 400: Inventory in logistics area not available"
        inv = MockInventoryClient({p: 100.0 for p in products})
        with pytest.raises(SAPOutboundDeliveryError):
            stock_transfer_service.try_post_goods_issue(db, out, inv, sto_id)
        doc = db[STO_COLLECTION].find_one({"_id": sto_id})
        assert doc["gi_status"] == "failed"
        assert "Inventory in logistics area" in doc["gi_error"]
        assert doc["outbound_delivery_object_ids"] == ["E0", "E1"]

    def test_unmatched_product_line_is_still_posted(self, db, sto_factory):
        """Edge case: SAP delivery item whose product_id doesn't match any
        stored STO line (e.g. RayItemcode_KUT blank) - current code posts GI
        with NO stock pre-check. Documents actual behaviour."""
        sto_id = sto_factory([_line("KNOWN")])
        out = MockOutboundClient([[_delivery_item("U1", None)]])
        inv = MockInventoryClient({"KNOWN": 0.0})
        result = stock_transfer_service.try_post_goods_issue(db, out, inv, sto_id)
        assert result == "posted"
        assert out.gi_calls == ["U1"]


# ------------------------------------- ROOT CAUSE 2 (company cache pcode)
class TestCompanyCachePcodeFallback:
    def test_seeded_cache_has_w2_with_pcode_p2w(self, db):
        doc = db["erp_company_cache"].find_one({"_id": "latest"})
        assert doc is not None, "erp_company_cache/latest missing"
        companies = doc["companies"]
        assert "W2" in companies
        assert companies["W2"]["pcode"] == "P2W"

    def test_p2w_resolves_via_pcode_fallback(self, db):
        result = company_cache_service.get_cached_company_info(db, ["P2W"], erp_portal_client=None)
        assert "P2W" in result, f"P2W not resolved: {result}"
        rec = result["P2W"]
        assert rec.get("gstin"), "gstin must not be null for P2W"
        assert rec.get("pan"), "pan must not be null for P2W"
        w2 = db["erp_company_cache"].find_one({"_id": "latest"})["companies"]["W2"]
        assert rec["gstin"] == w2["gstin"]
        assert rec["pan"] == w2["pan"]

    def test_direct_ccode_sites_still_resolve(self, db):
        companies = db["erp_company_cache"].find_one({"_id": "latest"})["companies"]
        result = company_cache_service.get_cached_company_info(db, ["P1", "W1"], erp_portal_client=None)
        assert set(result) == {"P1", "W1"}
        assert result["P1"]["gstin"] == companies["P1"]["gstin"]
        assert result["P1"]["pan"] == companies["P1"]["pan"]
        # W1's own Ccode entry must win over any pcode index collision
        assert result["W1"]["pcode"] == companies["W1"]["pcode"]
        assert result["W1"]["gstin"] == companies["W1"]["gstin"]

    def test_multi_code_mixed_lookup_and_lowercase(self, db):
        result = company_cache_service.get_cached_company_info(db, ["p2w", "P1"], erp_portal_client=None)
        assert set(result) == {"P2W", "P1"}, result

    def test_unknown_code_omitted_no_crash(self, db):
        result = company_cache_service.get_cached_company_info(db, ["ZZZNOPE"], erp_portal_client=None)
        assert result == {}


# ----------------------------------------------------- API regression
@pytest.fixture(scope="module")
def session_cookies(db):
    """Synthetic Entra-SSO session (app has no password login) - super_admin
    so page permissions don't interfere. Cleaned up after the module."""
    from datetime import datetime, timedelta, timezone

    user_id = "testtid:testoid-iter127"
    token = "TEST_iter127_sto_session"
    db["auth_users"].replace_one({"_id": user_id}, {
        "_id": user_id, "tid": "testtid", "oid": "testoid-iter127",
        "email": "qa.iter127@example.test", "name": "QA 127",
        "role": "super_admin", "allowed_pages": [], "bound_sites": [],
        "created_at": datetime.now(timezone.utc), "last_login_at": datetime.now(timezone.utc),
    }, upsert=True)
    db["auth_sessions"].replace_one({"_id": token}, {
        "_id": token, "user_id": user_id,
        "expires_at": datetime.now(timezone.utc) + timedelta(days=1),
    }, upsert=True)
    yield {"vms_session": token}
    db["auth_sessions"].delete_one({"_id": token})
    db["auth_users"].delete_one({"_id": user_id})


class TestStockTransferApiRegression:
    def test_orders_list_returns_200_with_both_object_id_fields(self, session_cookies):
        resp = requests.get(f"{BASE_URL}/api/stock-transfer/orders", cookies=session_cookies, timeout=60)
        assert resp.status_code == 200, resp.text[:300]
        orders = resp.json()
        assert isinstance(orders, list) and orders
        posted = [o for o in orders if o.get("gi_status") == "posted"]
        assert posted, "no already-posted orders found for regression check"
        for o in posted[:5]:
            assert "outbound_delivery_object_id" in o, f"{o['sto_id']} missing singular field"
            assert "_id" not in o, "raw Mongo _id leaked in response"

    @pytest.mark.parametrize("sto_id", ["STO-000042", "STO-000045"])
    def test_delivery_note_still_200(self, sto_id, session_cookies):
        resp = requests.get(f"{BASE_URL}/api/stock-transfer/{sto_id}/delivery-note", cookies=session_cookies, timeout=90)
        assert resp.status_code == 200, f"{sto_id}: HTTP {resp.status_code} {resp.text[:300]}"
        data = resp.json()
        assert data.get("items") is not None
        assert "_id" not in data
