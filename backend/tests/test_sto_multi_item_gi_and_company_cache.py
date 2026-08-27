"""Aug 27 2026 bug-fix verification.

Iteration 127 (original): multi-item Goods Issue - post GI for EVERY line.
Iteration 128 (this update): the GI assertions below were rewritten for the
NEW per-DOCUMENT design - post_goods_issue must be called ONCE per distinct
`parent_object_id` (one real Outbound Delivery Request document), not once
per line - plus the new human-readable Outbound Delivery ID lookup
(sap_outbound_delivery_client.find_outbound_delivery_ids ->
STO.outbound_delivery_ids).

Everything SAP-related is FULLY MOCKED - never touches the live tenant.
Also kept: company_cache_service pcode fallback (real read-only Mongo) and
the stock-transfer API regression checks.
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
import sap_outbound_delivery_client as odc_module  # noqa: E402
from sap_outbound_delivery_client import SAPOutboundDeliveryClient, SAPOutboundDeliveryError  # noqa: E402

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

    def __init__(self, responses, delivery_ids_by_uuid=None):
        # responses: list of "delivery items" lists, one per poll tick
        self._responses = list(responses)
        self.find_calls = []
        self.gi_calls = []
        self.gi_error = None
        self.delivery_id_calls = []
        self.delivery_ids_by_uuid = delivery_ids_by_uuid or {}
        self.delivery_id_error = None

    def find_delivery_request_items(self, uuid_):
        self.find_calls.append(uuid_)
        idx = min(len(self.find_calls) - 1, len(self._responses) - 1)
        return self._responses[idx]

    def post_goods_issue(self, object_id):
        self.gi_calls.append(object_id)
        if self.gi_error:
            raise SAPOutboundDeliveryError(self.gi_error)
        return {"ok": True}

    def find_outbound_delivery_ids(self, item_uuids):
        self.delivery_id_calls.append(list(item_uuids))
        if self.delivery_id_error:
            raise SAPOutboundDeliveryError(self.delivery_id_error)
        out = []
        for u in item_uuids:
            did = self.delivery_ids_by_uuid.get(u)
            if did and did not in out:
                out.append(did)
        return out


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


def _delivery_item(object_id, product_id, status="1", parent_object_id="DOC1", item_uuid=None):
    """`parent_object_id` = the ONE Outbound Delivery Request DOCUMENT the
    line belongs to (shared by every line SAP put on the same document)."""
    return {
        "object_id": object_id,
        "parent_object_id": parent_object_id,
        "uuid": item_uuid or f"UUID-{object_id}",
        "order_fulfilment_status": status,
        "product_id": product_id,
        "description": f"desc {product_id}",
    }


@pytest.fixture
def sto_factory(db):
    created = []

    def _make(items, gi_status="awaiting_delivery", ship_from="P2W", extra=None):
        sto_id = f"TEST_STO_{uuid.uuid4().hex[:8]}"
        payload = {
            "_id": sto_id,
            "status": "created_in_sap",
            "gi_status": gi_status,
            "ship_from_site_id": ship_from,
            "ship_to_site_id": "P1",
            "sap_order_id": "TEST30336",
            "sap_order_uuid": str(uuid.uuid4()).upper(),
            "items": items,
        }
        payload.update(extra or {})
        db[STO_COLLECTION].insert_one(payload)
        created.append(sto_id)
        return sto_id

    yield _make
    for sid in created:
        db[STO_COLLECTION].delete_one({"_id": sid})


def _line(pid, qty=10.0, wh="RM01"):
    return {"product_id": pid, "requested_qty": qty, "source_warehouse_id": wh, "description": pid}


# ------------------- FIX 1: one combined delivery per SAP document ------
class TestSingleCombinedDeliveryGrouping:
    """post_goods_issue must fire ONCE per distinct parent_object_id."""

    def test_three_lines_one_parent_posts_exactly_once(self, db, sto_factory):
        """(a) real STO-000046 shape: 3 lines, one shared document."""
        products = ["HRPIPE3329", "MSPIPE402015", "MSPIPE408MM"]
        sto_id = sto_factory([_line(p) for p in products])
        items = [_delivery_item(f"OID{i}", p, parent_object_id="PARENT-A") for i, p in enumerate(products)]
        out = MockOutboundClient([items], delivery_ids_by_uuid={f"UUID-OID{i}": "P8D1-185" for i in range(3)})
        inv = MockInventoryClient({p: 100.0 for p in products})

        result = stock_transfer_service.try_post_goods_issue(db, out, inv, sto_id)

        assert result == "posted", f"expected posted, got {result}"
        assert out.gi_calls == ["PARENT-A"], f"expected exactly 1 post with the parent id, got {out.gi_calls}"

        doc = db[STO_COLLECTION].find_one({"_id": sto_id})
        assert doc["gi_status"] == "posted"
        assert doc["gi_error"] is None
        assert doc["gi_job_running"] is False
        assert doc["outbound_delivery_ids"] == ["P8D1-185"]
        # the ID lookup must be called with THIS group's line UUIDs
        assert out.delivery_id_calls == [[f"UUID-OID{i}" for i in range(3)]]

    def test_two_distinct_parents_post_twice_with_own_groups(self, db, sto_factory):
        """(b) SAP legitimately split into 2 documents -> 2 posts."""
        products = ["P-A", "P-B", "P-C"]
        sto_id = sto_factory([_line(p) for p in products])
        items = [
            _delivery_item("OID0", "P-A", parent_object_id="DOC-1", item_uuid="U0"),
            _delivery_item("OID1", "P-B", parent_object_id="DOC-1", item_uuid="U1"),
            _delivery_item("OID2", "P-C", parent_object_id="DOC-2", item_uuid="U2"),
        ]
        out = MockOutboundClient([items], delivery_ids_by_uuid={"U0": "P8D1-185", "U1": "P8D1-185", "U2": "P8D1-186"})
        inv = MockInventoryClient({p: 100.0 for p in products})

        result = stock_transfer_service.try_post_goods_issue(db, out, inv, sto_id)

        assert result == "posted"
        assert sorted(out.gi_calls) == ["DOC-1", "DOC-2"], out.gi_calls
        assert len(out.gi_calls) == 2
        doc = db[STO_COLLECTION].find_one({"_id": sto_id})
        assert doc["outbound_delivery_ids"] == ["P8D1-185", "P8D1-186"], doc["outbound_delivery_ids"]

    def test_one_short_line_blocks_its_whole_group(self, db, sto_factory):
        """(c) one short line -> its ENTIRE document is not posted; a
        second, fully-available document still posts."""
        sto_id = sto_factory([_line("G1-A"), _line("G1-SHORT"), _line("G2-A")])
        items = [
            _delivery_item("OID0", "G1-A", parent_object_id="DOC-1", item_uuid="U0"),
            _delivery_item("OID1", "G1-SHORT", parent_object_id="DOC-1", item_uuid="U1"),
            _delivery_item("OID2", "G2-A", parent_object_id="DOC-2", item_uuid="U2"),
        ]
        out = MockOutboundClient([items], delivery_ids_by_uuid={"U2": "P8D1-190"})
        inv = MockInventoryClient({"G1-A": 100.0, "G1-SHORT": 1.0, "G2-A": 100.0})

        result = stock_transfer_service.try_post_goods_issue(db, out, inv, sto_id)

        assert result == "waiting", f"expected waiting, got {result}"
        assert out.gi_calls == ["DOC-2"], f"short group must not post at all: {out.gi_calls}"
        doc = db[STO_COLLECTION].find_one({"_id": sto_id})
        assert doc["gi_status"] == "insufficient_stock"
        assert "G1-SHORT" in doc["gi_error"], doc["gi_error"]
        assert "RM01" in doc["gi_error"], doc["gi_error"]
        assert "G1-A" not in doc["gi_error"], "only the genuinely short line should be named"
        assert doc["outbound_delivery_ids"] == ["P8D1-190"]

    def test_single_item_regression_posts_once(self, db, sto_factory):
        """(d) single-line STO still posts exactly once and reaches posted."""
        sto_id = sto_factory([_line("HRPIPE3329", qty=5.0)])
        out = MockOutboundClient(
            [[_delivery_item("SOLO1", "HRPIPE3329", parent_object_id="SOLO-DOC", item_uuid="SU1")]],
            delivery_ids_by_uuid={"SU1": "P1D1-900"},
        )
        inv = MockInventoryClient({"HRPIPE3329": 50.0})
        assert stock_transfer_service.try_post_goods_issue(db, out, inv, sto_id) == "posted"
        assert out.gi_calls == ["SOLO-DOC"]
        doc = db[STO_COLLECTION].find_one({"_id": sto_id})
        assert doc["gi_status"] == "posted"
        assert doc["outbound_delivery_object_id"] == "SOLO1"
        assert doc["outbound_delivery_object_ids"] == ["SOLO1"]
        assert doc["outbound_delivery_ids"] == ["P1D1-900"]

    def test_finished_group_not_reposted_next_tick_and_ids_accumulate(self, db, sto_factory):
        """(e) tick 2: group 1 is already Finished ('3'), group 2 newly
        ready -> only group 2 posts, and the delivery ID from tick 1 is
        NOT lost."""
        sto_id = sto_factory([_line("G1-A"), _line("G1-B"), _line("G2-A")])
        tick1 = [
            _delivery_item("OID0", "G1-A", parent_object_id="DOC-1", item_uuid="U0"),
            _delivery_item("OID1", "G1-B", parent_object_id="DOC-1", item_uuid="U1"),
            _delivery_item("OID2", "G2-A", parent_object_id="DOC-2", item_uuid="U2"),
        ]
        tick2 = [
            _delivery_item("OID0", "G1-A", status="3", parent_object_id="DOC-1", item_uuid="U0"),
            _delivery_item("OID1", "G1-B", status="3", parent_object_id="DOC-1", item_uuid="U1"),
            _delivery_item("OID2", "G2-A", parent_object_id="DOC-2", item_uuid="U2"),
        ]
        out = MockOutboundClient([tick1, tick2], delivery_ids_by_uuid={"U0": "P8D1-185", "U1": "P8D1-185", "U2": "P8D1-186"})
        inv = MockInventoryClient({"G1-A": 100.0, "G1-B": 100.0, "G2-A": 0.0})

        # tick 1: group 2 short -> only group 1 posts
        assert stock_transfer_service.try_post_goods_issue(db, out, inv, sto_id) == "waiting"
        assert out.gi_calls == ["DOC-1"], out.gi_calls
        doc = db[STO_COLLECTION].find_one({"_id": sto_id})
        assert doc["outbound_delivery_ids"] == ["P8D1-185"]

        # tick 2: group 1 is Finished, group 2 replenished
        inv.stock_by_product["G2-A"] = 500.0
        assert stock_transfer_service.try_post_goods_issue(db, out, inv, sto_id) == "posted"
        assert out.gi_calls == ["DOC-1", "DOC-2"], f"finished group must not be re-posted: {out.gi_calls}"
        assert out.delivery_id_calls[-1] == ["U2"], out.delivery_id_calls
        doc = db[STO_COLLECTION].find_one({"_id": sto_id})
        assert doc["gi_status"] == "posted"
        assert doc["gi_error"] is None
        assert doc["outbound_delivery_ids"] == ["P8D1-185", "P8D1-186"], (
            f"previous group's delivery ID must be preserved: {doc['outbound_delivery_ids']}"
        )

    def test_all_finished_marks_posted_without_any_gi_call(self, db, sto_factory):
        products = ["A1", "A2"]
        sto_id = sto_factory([_line(p) for p in products])
        items = [_delivery_item(f"F{i}", p, status="3") for i, p in enumerate(products)]
        out = MockOutboundClient([items])
        inv = MockInventoryClient({p: 100.0 for p in products})
        assert stock_transfer_service.try_post_goods_issue(db, out, inv, sto_id) == "posted"
        assert out.gi_calls == [], "no GI should be posted when every line is already Finished"

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
        """SAP rejects the one document -> gi_status failed, error raised.
        Updated for grouping: exactly 1 post attempt (per document)."""
        products = ["B1", "B2"]
        sto_id = sto_factory([_line(p) for p in products])
        items = [_delivery_item(f"E{i}", p, parent_object_id="DOC-X") for i, p in enumerate(products)]
        out = MockOutboundClient([items])
        out.gi_error = "HTTP 400: Inventory in logistics area not available"
        inv = MockInventoryClient({p: 100.0 for p in products})
        with pytest.raises(SAPOutboundDeliveryError):
            stock_transfer_service.try_post_goods_issue(db, out, inv, sto_id)
        assert out.gi_calls == ["DOC-X"], out.gi_calls
        doc = db[STO_COLLECTION].find_one({"_id": sto_id})
        assert doc["gi_status"] == "failed"
        assert "Inventory in logistics area" in doc["gi_error"]
        assert doc["outbound_delivery_object_ids"] == ["E0", "E1"]

    def test_missing_parent_object_id_degrades_to_per_line(self, db, sto_factory):
        """Defensive: if SAP omits ParentObjectID the code must still post
        (falls back to the line's own object_id) rather than crash."""
        products = ["N1", "N2"]
        sto_id = sto_factory([_line(p) for p in products])
        items = [_delivery_item(f"OID{i}", p, parent_object_id=None) for i, p in enumerate(products)]
        out = MockOutboundClient([items])
        inv = MockInventoryClient({p: 100.0 for p in products})
        assert stock_transfer_service.try_post_goods_issue(db, out, inv, sto_id) == "posted"
        assert sorted(out.gi_calls) == ["OID0", "OID1"], out.gi_calls

    def test_unmatched_product_line_does_not_block_group(self, db, sto_factory):
        """SAP delivery item whose product_id doesn't match any stored STO
        line (blank RayItemcode_KUT) - posted with NO stock pre-check.
        Documents actual behaviour."""
        sto_id = sto_factory([_line("KNOWN")])
        out = MockOutboundClient([[_delivery_item("U1", None, parent_object_id="DOC-U")]])
        inv = MockInventoryClient({"KNOWN": 0.0})
        assert stock_transfer_service.try_post_goods_issue(db, out, inv, sto_id) == "posted"
        assert out.gi_calls == ["DOC-U"]


# ------------ FIX 2: cosmetic delivery-ID lookup must never fail the GI --
class TestDeliveryIdLookupIsolation:
    def test_lookup_error_keeps_gi_posted(self, db, sto_factory):
        sto_id = sto_factory([_line("Q1"), _line("Q2")])
        items = [_delivery_item(f"OID{i}", p, parent_object_id="DOC-Q") for i, p in enumerate(["Q1", "Q2"])]
        out = MockOutboundClient([items])
        out.delivery_id_error = "HTTP 500: OData blew up"
        inv = MockInventoryClient({"Q1": 100.0, "Q2": 100.0})

        result = stock_transfer_service.try_post_goods_issue(db, out, inv, sto_id)

        assert result == "posted", "cosmetic ID lookup failure must not change the GI outcome"
        doc = db[STO_COLLECTION].find_one({"_id": sto_id})
        assert doc["gi_status"] == "posted", doc["gi_status"]
        assert doc["gi_error"] is None
        assert doc.get("outbound_delivery_ids") == []

    def test_lookup_retried_on_next_tick_after_failure(self, db, sto_factory):
        """gi_status posted short-circuits, so verify the retry path on an
        order left 'waiting' (a second group still pending)."""
        sto_id = sto_factory([_line("R1"), _line("R2")])
        tick1 = [
            _delivery_item("OID0", "R1", parent_object_id="DOC-1", item_uuid="RU0"),
            _delivery_item("OID1", "R2", parent_object_id="DOC-2", item_uuid="RU1"),
        ]
        tick2 = [
            _delivery_item("OID0", "R1", status="3", parent_object_id="DOC-1", item_uuid="RU0"),
            _delivery_item("OID1", "R2", parent_object_id="DOC-2", item_uuid="RU1"),
        ]
        out = MockOutboundClient([tick1, tick2], delivery_ids_by_uuid={"RU0": "P8D1-201", "RU1": "P8D1-202"})
        out.delivery_id_error = "HTTP 500: transient"
        inv = MockInventoryClient({"R1": 100.0, "R2": 0.0})

        assert stock_transfer_service.try_post_goods_issue(db, out, inv, sto_id) == "waiting"
        doc = db[STO_COLLECTION].find_one({"_id": sto_id})
        assert doc["outbound_delivery_ids"] == [], "failed lookup should not persist bogus ids"

        out.delivery_id_error = None
        inv.stock_by_product["R2"] = 500.0
        assert stock_transfer_service.try_post_goods_issue(db, out, inv, sto_id) == "posted"
        doc = db[STO_COLLECTION].find_one({"_id": sto_id})
        # tick 2 only looks up the newly posted group's uuid
        assert "P8D1-202" in doc["outbound_delivery_ids"], doc["outbound_delivery_ids"]


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


class TestFindOutboundDeliveryIdsClient:
    """Real client method against a MOCKED OData HTTP response shaped like
    the live one (ItemUUID filter + $expand=OutboundDelivery)."""

    @staticmethod
    def _client():
        return SAPOutboundDeliveryClient("https://sap.example.test/odataoutboundemergent", "u", "p", "vhost")

    def test_returns_deduped_ids_and_uses_itemuuid_filter(self, monkeypatch):
        captured = {}

        def fake_get(url, params=None, auth=None, headers=None, timeout=None):
            captured["url"] = url
            captured["params"] = params
            return _FakeResponse({"d": {"results": [
                {"ItemUUID": "FA163E88-19FA-1FD1-A8C3-09E96C13D093", "OutboundDelivery": {"ID": "P8D1-185", "ObjectID": "OBJ1"}},
                {"ItemUUID": "FA163E88-19FA-1FD1-A8C3-09E96C13F093", "OutboundDelivery": {"ID": "P8D1-185", "ObjectID": "OBJ1"}},
                {"ItemUUID": "FA163E88-19FA-1FD1-A8C3-09E96C141093", "OutboundDelivery": {"ID": "P8D1-186", "ObjectID": "OBJ2"}},
            ]}})

        monkeypatch.setattr(odc_module.requests, "get", fake_get)
        uuids = [
            "FA163E88-19FA-1FD1-A8C3-09E96C13D093",
            "FA163E88-19FA-1FD1-A8C3-09E96C13F093",
            "FA163E88-19FA-1FD1-A8C3-09E96C141093",
        ]
        ids = self._client().find_outbound_delivery_ids(uuids)
        assert ids == ["P8D1-185", "P8D1-186"], ids
        assert "OutboundDeliveryItemBusinessTransactionDocumentReferenceOutboundDeliveryRequestC" in captured["url"]
        assert captured["params"]["$expand"] == "OutboundDelivery"
        for u in uuids:
            assert f"ItemUUID eq guid'{u}'" in captured["params"]["$filter"], captured["params"]["$filter"]
        assert "UUID eq guid" not in captured["params"]["$filter"].replace("ItemUUID eq guid", "")

    def test_deferred_or_missing_delivery_is_skipped(self, monkeypatch):
        monkeypatch.setattr(odc_module.requests, "get", lambda *a, **k: _FakeResponse({"d": {"results": [
            {"OutboundDelivery": {"__deferred": {"uri": "x"}}},
            {"OutboundDelivery": None},
            {},
        ]}}))
        assert self._client().find_outbound_delivery_ids(["U1"]) == []

    def test_empty_uuid_list_makes_no_http_call(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("no HTTP call expected")

        monkeypatch.setattr(odc_module.requests, "get", boom)
        assert self._client().find_outbound_delivery_ids([]) == []
        assert self._client().find_outbound_delivery_ids([None, ""]) == []

    def test_http_error_raises_sap_error(self, monkeypatch):
        monkeypatch.setattr(odc_module.requests, "get", lambda *a, **k: _FakeResponse({"error": "nope"}, status_code=500))
        with pytest.raises(SAPOutboundDeliveryError):
            self._client().find_outbound_delivery_ids(["U1"])


class TestFindDeliveryRequestItemsParentId:
    def test_parent_object_id_and_uuid_returned(self, monkeypatch):
        monkeypatch.setattr(odc_module.requests, "get", lambda *a, **k: _FakeResponse({"d": {"results": [
            {"OutboundDeliveryRequestItem": {
                "ObjectID": "ITEM1", "ParentObjectID": "PARENT1", "UUID": "U1",
                "OrderFulfilmentProcessingStatusCode": "1", "RayItemcode_KUT": "P-A",
                "RAYITEMDESCRIPTION_KUT": "desc A"}},
            {"OutboundDeliveryRequestItem": {
                "ObjectID": "ITEM2", "ParentObjectID": "PARENT1", "UUID": "U2",
                "OrderFulfilmentProcessingStatusCode": "3", "RayItemcode_KUT": "P-B",
                "RAYITEMDESCRIPTION_KUT": "desc B"}},
            {"OutboundDeliveryRequestItem": {"__deferred": {"uri": "x"}}},
        ]}}))
        client = SAPOutboundDeliveryClient("https://sap.example.test/odataoutboundemergent", "u", "p", "vhost")
        rows = client.find_delivery_request_items("aabbccdd-0000-0000-0000-000000000000")
        assert len(rows) == 2, rows
        assert rows[0]["parent_object_id"] == "PARENT1"
        assert rows[1]["parent_object_id"] == "PARENT1"
        assert rows[0]["uuid"] == "U1"
        assert rows[0]["object_id"] == "ITEM1"
        assert rows[1]["order_fulfilment_status"] == "3"
        assert len({r["parent_object_id"] for r in rows}) == 1, "all lines of one document share one parent"


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
    def test_single_order_get_returns_200(self, sto_id, session_cookies):
        resp = requests.get(f"{BASE_URL}/api/stock-transfer/orders/{sto_id}", cookies=session_cookies, timeout=60)
        assert resp.status_code == 200, f"{sto_id}: HTTP {resp.status_code} {resp.text[:300]}"
        data = resp.json()
        assert data.get("sto_id") == sto_id
        assert "_id" not in data
        # pre-fix orders may not carry the new plural field at all - the UI
        # must fall back to the singular one; either shape is acceptable.
        ids = data.get("outbound_delivery_ids")
        assert ids is None or isinstance(ids, list), f"{sto_id}: outbound_delivery_ids wrong type {ids!r}"
        if not ids:
            assert data.get("outbound_delivery_object_id"), (
                f"{sto_id} is posted but has neither new nor legacy delivery reference"
            )

    def test_new_plural_field_surfaces_through_api(self, db, session_cookies, sto_factory):
        """A newly-posted (mocked) order must expose outbound_delivery_ids
        through the API exactly as the UI expects."""
        sto_id = sto_factory([_line("API-A"), _line("API-B")])
        items = [_delivery_item(f"OID{i}", p, parent_object_id="DOC-API", item_uuid=f"AU{i}")
                 for i, p in enumerate(["API-A", "API-B"])]
        out = MockOutboundClient([items], delivery_ids_by_uuid={"AU0": "P8D1-185", "AU1": "P8D1-185"})
        inv = MockInventoryClient({"API-A": 100.0, "API-B": 100.0})
        assert stock_transfer_service.try_post_goods_issue(db, out, inv, sto_id) == "posted"

        resp = requests.get(f"{BASE_URL}/api/stock-transfer/orders/{sto_id}", cookies=session_cookies, timeout=60)
        assert resp.status_code == 200, resp.text[:300]
        data = resp.json()
        assert data["outbound_delivery_ids"] == ["P8D1-185"], data.get("outbound_delivery_ids")
        assert data["gi_status"] == "posted"

    @pytest.mark.parametrize("sto_id", ["STO-000042", "STO-000045"])
    def test_delivery_note_still_200(self, sto_id, session_cookies):
        resp = requests.get(f"{BASE_URL}/api/stock-transfer/{sto_id}/delivery-note", cookies=session_cookies, timeout=90)
        assert resp.status_code == 200, f"{sto_id}: HTTP {resp.status_code} {resp.text[:300]}"
        data = resp.json()
        assert data.get("items") is not None
        assert "_id" not in data
