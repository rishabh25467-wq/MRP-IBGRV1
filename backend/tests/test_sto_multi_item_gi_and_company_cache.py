"""Aug 27 2026 bug-fix verification.

Iteration 127 (original): multi-item Goods Issue - post GI for EVERY line.
Iteration 128: briefly tried a per-DOCUMENT design (post_goods_issue once
per shared parent_object_id) - REVERTED same day after a real SAP
rejection ("object does not exist", confirmed via $metadata that
PGIInBackground is scoped to OutboundDeliveryRequestItemCollection).
Back to per-LINE posting (iteration 127's original design), keeping the
human-readable Outbound Delivery ID lookup added in iteration 128.

Attempt #5 (real incident: order 30411 - confirmed the STO-creation fix
DOES combine lines into one shared Outbound Delivery Request, but
PGIInBackground still posts each line to its OWN separate Delivery):
post_goods_issue now always posts with `auto_release=False`, and a new
`_release_ready_deliveries` helper explicitly finds + releases whatever
Delivery object(s) result, only once every line has resolved to one.

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
    """Fakes ONLY the HTTP layer's return values - no network calls.

    `delivery_objects_by_uuid`: {item_uuid: (object_id, human_id)} - the
    single source of truth for BOTH find_outbound_delivery_ids (legacy,
    still used by a couple of client-level tests) and
    find_outbound_delivery_objects (attempt #5, drives release calls).
    Two item_uuids mapped to the SAME object_id simulate a successfully
    COMBINED delivery."""

    def __init__(self, responses, delivery_objects_by_uuid=None):
        # responses: list of "delivery items" lists, one per poll tick
        self._responses = list(responses)
        self.find_calls = []
        self.gi_calls = []  # list of (object_id, auto_release)
        self.gi_error = None
        self.delivery_id_calls = []
        self.delivery_objects_by_uuid = delivery_objects_by_uuid or {}
        self.delivery_id_error = None
        self.release_calls = []
        self.release_error = None
        self.request_delivery_execution_calls = []
        self.request_delivery_execution_error = None
        self.schedule_line_gi_calls = []
        self.schedule_line_gi_error = None
        self.schedule_line_gi_succeeded = []

    def find_delivery_request_items(self, uuid_):
        self.find_calls.append(uuid_)
        idx = min(len(self.find_calls) - 1, len(self._responses) - 1)
        return self._responses[idx]

    def post_goods_issue(self, object_id, auto_release=False):
        self.gi_calls.append((object_id, auto_release))
        if self.gi_error:
            raise SAPOutboundDeliveryError(self.gi_error)
        return {"ok": True}

    def find_outbound_delivery_ids(self, item_uuids):
        self.delivery_id_calls.append(list(item_uuids))
        if self.delivery_id_error:
            raise SAPOutboundDeliveryError(self.delivery_id_error)
        out = []
        for u in item_uuids:
            entry = self.delivery_objects_by_uuid.get(u)
            did = entry[1] if entry else None
            if did and did not in out:
                out.append(did)
        return out

    def find_outbound_delivery_objects(self, item_uuids):
        self.delivery_id_calls.append(list(item_uuids))
        if self.delivery_id_error:
            raise SAPOutboundDeliveryError(self.delivery_id_error)
        results = []
        for u in item_uuids:
            entry = self.delivery_objects_by_uuid.get(u)
            if entry:
                results.append({"object_id": entry[0], "id": entry[1], "item_uuid": u})
        return results

    def release_outbound_delivery(self, delivery_object_id):
        self.release_calls.append(delivery_object_id)
        if self.release_error:
            raise SAPOutboundDeliveryError(self.release_error)
        return {"ok": True}

    def request_delivery_execution(self, schedule_line_object_id, target_uuid):
        self.request_delivery_execution_calls.append((schedule_line_object_id, target_uuid))
        if self.request_delivery_execution_error:
            raise SAPOutboundDeliveryError(self.request_delivery_execution_error)
        return {"ok": True}

    def post_goods_issue_schedule_line(self, schedule_line_object_id):
        self.schedule_line_gi_calls.append(schedule_line_object_id)
        if self.schedule_line_gi_error:
            raise SAPOutboundDeliveryError(self.schedule_line_gi_error)
        self.schedule_line_gi_succeeded.append(schedule_line_object_id)
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


def _delivery_item(object_id, product_id, status="1", parent_object_id="DOC1", item_uuid=None, schedule_line_object_id=None):
    """`parent_object_id` = the ONE Outbound Delivery Request DOCUMENT the
    line belongs to (shared by every line SAP put on the same document).
    `schedule_line_object_id` (attempt #6) = this line's own
    OutboundDeliveryRequestItemScheduleLine ObjectID, needed for the new
    SLRequestDeliveryExecution combine attempt - omitted (None) by every
    pre-existing test on purpose, matching real pre-fix STO docs that
    never captured it."""
    return {
        "object_id": object_id,
        "parent_object_id": parent_object_id,
        "uuid": item_uuid or f"UUID-{object_id}",
        "order_fulfilment_status": status,
        "product_id": product_id,
        "description": f"desc {product_id}",
        "schedule_line_object_id": schedule_line_object_id,
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


# ------------- FIX 1 (reverted): post_goods_issue is per-LINE again ------
class TestPerLineGoodsIssuePosting:
    """Aug 27 2026: a same-session 2nd attempt tried grouping by
    parent_object_id and posting ONCE per document - live SAP rejected it
    ("Action PGIInBackground not possible; object does not exist", real
    incident STO-000047/order 30384), confirmed via this OData service's
    own $metadata that PGIInBackground is scoped to
    OutboundDeliveryRequestItemCollection (item-level only). Reverted:
    post_goods_issue must fire once per still-open LINE (its own
    item-level object_id) - every line still reliably ships. Attempt #5
    (see module docstring) changed HOW that per-line post is released:
    always `auto_release=False` now, with an explicit
    release_outbound_delivery call driven by
    _release_ready_deliveries."""

    def test_three_lines_all_sufficient_posts_three_times(self, db, sto_factory):
        products = ["HRPIPE3329", "MSPIPE402015", "MSPIPE408MM"]
        sto_id = sto_factory([_line(p) for p in products])
        items = [_delivery_item(f"OID{i}", p, item_uuid=f"U{i}") for i, p in enumerate(products)]
        out = MockOutboundClient([items], delivery_objects_by_uuid={f"U{i}": (f"OBJ{185+i}", f"P8D1-{185+i}") for i in range(3)})
        inv = MockInventoryClient({p: 100.0 for p in products})

        result = stock_transfer_service.try_post_goods_issue(db, out, inv, sto_id)

        assert result == "posted", f"expected posted, got {result}"
        assert sorted(oc[0] for oc in out.gi_calls) == ["OID0", "OID1", "OID2"], f"expected one post per line, got {out.gi_calls}"
        assert all(auto_release is False for _, auto_release in out.gi_calls), "GI must never auto-release inline anymore"
        assert sorted(out.release_calls) == ["OBJ185", "OBJ186", "OBJ187"], f"3 separate Deliveries -> 3 explicit releases: {out.release_calls}"

        doc = db[STO_COLLECTION].find_one({"_id": sto_id})
        assert doc["gi_status"] == "posted"
        assert doc["gi_error"] is None
        assert doc["gi_job_running"] is False
        assert sorted(doc["outbound_delivery_ids"]) == ["P8D1-185", "P8D1-186", "P8D1-187"]

    def test_lines_sharing_one_delivery_object_release_only_once(self, db, sto_factory):
        """Attempt #5's actual hypothesis under test: if SAP DOES resolve
        every line of a combined order to the SAME Delivery object, this
        must release it exactly once (not once per line)."""
        products = ["G12LW", "G12FW"]
        sto_id = sto_factory([_line(p) for p in products])
        items = [_delivery_item(f"OID{i}", p, item_uuid=f"U{i}") for i, p in enumerate(products)]
        out = MockOutboundClient([items], delivery_objects_by_uuid={"U0": ("OBJ-SHARED", "P1D1-481"), "U1": ("OBJ-SHARED", "P1D1-481")})
        inv = MockInventoryClient({p: 100.0 for p in products})

        result = stock_transfer_service.try_post_goods_issue(db, out, inv, sto_id)

        assert result == "posted"
        assert out.release_calls == ["OBJ-SHARED"], f"one shared Delivery must be released exactly once: {out.release_calls}"
        doc = db[STO_COLLECTION].find_one({"_id": sto_id})
        assert doc["outbound_delivery_ids"] == ["P1D1-481"], "must show ONE combined delivery ID, not a duplicate"

    def test_two_of_three_ready_one_short_then_next_tick_completes(self, db, sto_factory):
        sto_id = sto_factory([_line("G1-A"), _line("G1-B"), _line("G2-SHORT")])
        tick1 = [
            _delivery_item("OID0", "G1-A", item_uuid="U0"),
            _delivery_item("OID1", "G1-B", item_uuid="U1"),
            _delivery_item("OID2", "G2-SHORT", item_uuid="U2"),
        ]
        tick2 = [
            _delivery_item("OID0", "G1-A", status="3", item_uuid="U0"),
            _delivery_item("OID1", "G1-B", status="3", item_uuid="U1"),
            _delivery_item("OID2", "G2-SHORT", item_uuid="U2"),
        ]
        out = MockOutboundClient([tick1, tick2], delivery_objects_by_uuid={"U0": ("OBJ185", "P8D1-185"), "U1": ("OBJ186", "P8D1-186"), "U2": ("OBJ187", "P8D1-187")})
        inv = MockInventoryClient({"G1-A": 100.0, "G1-B": 100.0, "G2-SHORT": 1.0})

        # tick 1: the 2 ready lines post immediately, the short one waits
        result = stock_transfer_service.try_post_goods_issue(db, out, inv, sto_id)
        assert result == "waiting", f"expected waiting, got {result}"
        assert sorted(oc[0] for oc in out.gi_calls) == ["OID0", "OID1"], f"ready lines must still post even if a sibling is short: {out.gi_calls}"
        doc = db[STO_COLLECTION].find_one({"_id": sto_id})
        assert doc["gi_status"] == "insufficient_stock"
        assert "G2-SHORT" in doc["gi_error"], doc["gi_error"]
        assert "G1-A" not in doc["gi_error"], "only the genuinely short line should be named"
        assert sorted(doc["outbound_delivery_ids"]) == ["P8D1-185", "P8D1-186"]

        # tick 2: G1-A/G1-B already Finished (must not be re-posted), G2-SHORT now has stock
        inv.stock_by_product["G2-SHORT"] = 500.0
        result2 = stock_transfer_service.try_post_goods_issue(db, out, inv, sto_id)
        assert result2 == "posted"
        assert sorted(oc[0] for oc in out.gi_calls) == ["OID0", "OID1", "OID2"], f"finished lines must not be re-posted: {out.gi_calls}"
        doc = db[STO_COLLECTION].find_one({"_id": sto_id})
        assert doc["gi_status"] == "posted"
        assert sorted(doc["outbound_delivery_ids"]) == ["P8D1-185", "P8D1-186", "P8D1-187"]

    def test_single_item_regression_posts_once(self, db, sto_factory):
        sto_id = sto_factory([_line("HRPIPE3329", qty=5.0)])
        out = MockOutboundClient(
            [[_delivery_item("SOLO1", "HRPIPE3329", item_uuid="SU1")]],
            delivery_objects_by_uuid={"SU1": ("OBJ900", "P1D1-900")},
        )
        inv = MockInventoryClient({"HRPIPE3329": 50.0})
        assert stock_transfer_service.try_post_goods_issue(db, out, inv, sto_id) == "posted"
        assert out.gi_calls == [("SOLO1", False)]
        assert out.release_calls == ["OBJ900"]
        doc = db[STO_COLLECTION].find_one({"_id": sto_id})
        assert doc["gi_status"] == "posted"
        assert doc["outbound_delivery_object_id"] == "SOLO1"
        assert doc["outbound_delivery_object_ids"] == ["SOLO1"]
        assert doc["outbound_delivery_ids"] == ["P1D1-900"]

    def test_all_finished_marks_posted_without_any_gi_call(self, db, sto_factory):
        products = ["A1", "A2"]
        sto_id = sto_factory([_line(p) for p in products])
        items = [_delivery_item(f"F{i}", p, status="3", item_uuid=f"FU{i}") for i, p in enumerate(products)]
        out = MockOutboundClient([items], delivery_objects_by_uuid={"FU0": ("OBJF0", "P1D1-1"), "FU1": ("OBJF1", "P1D1-2")})
        inv = MockInventoryClient({p: 100.0 for p in products})
        assert stock_transfer_service.try_post_goods_issue(db, out, inv, sto_id) == "posted"
        assert out.gi_calls == [], "no GI should be posted when every line is already Finished"
        assert sorted(out.release_calls) == ["OBJF0", "OBJF1"], "already-Finished lines still need their Deliveries released once"

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

    def test_sap_rejection_on_one_line_does_not_block_the_other(self, db, sto_factory):
        """One line's post_goods_issue call fails - the other line must
        still be attempted (Aug 27 2026 hardening, unaffected by the
        per-document revert)."""
        products = ["B1", "B2"]
        sto_id = sto_factory([_line(p) for p in products])
        items = [_delivery_item(f"E{i}", p) for i, p in enumerate(products)]
        out = MockOutboundClient([items])
        out.gi_error = "HTTP 400: Inventory in logistics area not available"
        inv = MockInventoryClient({p: 100.0 for p in products})
        with pytest.raises(SAPOutboundDeliveryError):
            stock_transfer_service.try_post_goods_issue(db, out, inv, sto_id)
        assert sorted(oc[0] for oc in out.gi_calls) == ["E0", "E1"], out.gi_calls
        doc = db[STO_COLLECTION].find_one({"_id": sto_id})
        assert doc["gi_status"] == "failed"
        assert "Inventory in logistics area" in doc["gi_error"]
        assert doc["outbound_delivery_object_ids"] == ["E0", "E1"]

    def test_unmatched_product_line_is_still_posted(self, db, sto_factory):
        """SAP delivery item whose product_id doesn't match any stored STO
        line (blank RayItemcode_KUT) - posted with NO stock pre-check.
        Documents actual behaviour. GI posts immediately; "posted" itself
        waits until the Delivery object resolves (attempt #5)."""
        sto_id = sto_factory([_line("KNOWN")])
        out = MockOutboundClient([[_delivery_item("U1", None)]], delivery_objects_by_uuid={"UUID-U1": ("OBJU1", "P1D1-1")})
        inv = MockInventoryClient({"KNOWN": 0.0})
        assert stock_transfer_service.try_post_goods_issue(db, out, inv, sto_id) == "posted"
        assert out.gi_calls == [("U1", False)]


# ------------ FIX 2/5: release lookup must never fail the GI itself, but
# now DOES delay "posted" until the release attempt resolves (bounded) --
class TestDeliveryReleaseLookupIsolation:
    def test_lookup_error_delays_posted_until_release_succeeds(self, db, sto_factory):
        sto_id = sto_factory([_line("Q1"), _line("Q2")])
        tick1 = [_delivery_item("OID0", "Q1", item_uuid="QU0"), _delivery_item("OID1", "Q2", item_uuid="QU1")]
        tick2 = [_delivery_item("OID0", "Q1", status="3", item_uuid="QU0"), _delivery_item("OID1", "Q2", status="3", item_uuid="QU1")]
        out = MockOutboundClient([tick1, tick2], delivery_objects_by_uuid={"QU0": ("OBJQ0", "P1D1-1"), "QU1": ("OBJQ1", "P1D1-2")})
        out.delivery_id_error = "HTTP 500: OData blew up"
        inv = MockInventoryClient({"Q1": 100.0, "Q2": 100.0})

        result = stock_transfer_service.try_post_goods_issue(db, out, inv, sto_id)
        assert result == "waiting", "GI posted but the release lookup failed - must keep polling, not mark posted yet"
        assert sorted(oc[0] for oc in out.gi_calls) == ["OID0", "OID1"]
        doc = db[STO_COLLECTION].find_one({"_id": sto_id})
        assert doc.get("outbound_delivery_ids") == []

        out.delivery_id_error = None
        result2 = stock_transfer_service.try_post_goods_issue(db, out, inv, sto_id)
        assert result2 == "posted", "once the release lookup recovers, must finish and release"
        doc = db[STO_COLLECTION].find_one({"_id": sto_id})
        assert doc["gi_status"] == "posted"
        assert doc["gi_error"] is None
        assert sorted(doc["outbound_delivery_ids"]) == ["P1D1-1", "P1D1-2"]
        assert sorted(out.release_calls) == ["OBJQ0", "OBJQ1"]

    def test_lookup_retried_on_next_tick_after_failure(self, db, sto_factory):
        """Verify the retry path on an order left 'waiting' (a second line
        still pending on stock)."""
        sto_id = sto_factory([_line("R1"), _line("R2")])
        tick1 = [
            _delivery_item("OID0", "R1", item_uuid="RU0"),
            _delivery_item("OID1", "R2", item_uuid="RU1"),
        ]
        tick2 = [
            _delivery_item("OID0", "R1", status="3", item_uuid="RU0"),
            _delivery_item("OID1", "R2", item_uuid="RU1"),
        ]
        out = MockOutboundClient([tick1, tick2], delivery_objects_by_uuid={"RU0": ("OBJR0", "P8D1-201"), "RU1": ("OBJR1", "P8D1-202")})
        out.delivery_id_error = "HTTP 500: transient"
        inv = MockInventoryClient({"R1": 100.0, "R2": 0.0})

        assert stock_transfer_service.try_post_goods_issue(db, out, inv, sto_id) == "waiting"
        doc = db[STO_COLLECTION].find_one({"_id": sto_id})
        assert doc["outbound_delivery_ids"] == [], "failed lookup should not persist bogus ids"

        out.delivery_id_error = None
        inv.stock_by_product["R2"] = 500.0
        assert stock_transfer_service.try_post_goods_issue(db, out, inv, sto_id) == "posted"
        doc = db[STO_COLLECTION].find_one({"_id": sto_id})
        # tick 2 only looks up the newly posted line's uuid
        assert "P8D1-202" in doc["outbound_delivery_ids"], doc["outbound_delivery_ids"]


class TestScheduleLineGoodsIssueClient:
    """Real client methods against a MOCKED OData HTTP response."""

    def test_sl_pgi_in_background_sends_only_object_id(self, monkeypatch):
        captured = {}

        def fake_post(self, url, params=None, auth=None, headers=None, timeout=None):
            captured["url"] = url
            captured["params"] = params
            return _FakeResponse({"d": {"ObjectID": "SL1"}})

        def fake_get(self, url, params=None, auth=None, headers=None, timeout=None):
            resp = _FakeResponse({})
            resp.headers = {"x-csrf-token": "TOKEN123"}
            return resp

        monkeypatch.setattr(odc_module.requests.Session, "post", fake_post)
        monkeypatch.setattr(odc_module.requests.Session, "get", fake_get)

        client = SAPOutboundDeliveryClient("https://sap.example.test/odataoutboundemergent", "u", "p", "vhost")
        client.post_goods_issue_schedule_line("SL-OBJ-1")

        assert "SLPGIInBackground" in captured["url"]
        assert captured["params"]["ObjectID"] == "'SL-OBJ-1'"
        assert "TargetSiteLogisticsRequestUUID" not in captured["params"]

    def test_http_error_raises_sap_error(self, monkeypatch):
        def fake_get(self, url, params=None, auth=None, headers=None, timeout=None):
            resp = _FakeResponse({})
            resp.headers = {"x-csrf-token": "TOKEN123"}
            return resp

        monkeypatch.setattr(odc_module.requests.Session, "get", fake_get)
        monkeypatch.setattr(odc_module.requests.Session, "post", lambda self, *a, **k: _FakeResponse({"error": "bad"}, status_code=500))
        client = SAPOutboundDeliveryClient("https://sap.example.test/odataoutboundemergent", "u", "p", "vhost")
        with pytest.raises(SAPOutboundDeliveryError):
            client.post_goods_issue_schedule_line("SL-OBJ-1")


class TestFindDeliveryRequestItemsScheduleLineExpand:
    def test_nested_schedule_line_object_id_extracted_plain_array(self, monkeypatch):
        """Confirmed live (Aug 27 2026, real SAP order 30412): this nested
        expand returns a PLAIN JSON ARRAY for
        OutboundDeliveryRequestItemScheduleLine, NOT the usual
        {"results": [...]} wrapper every other collection in this service
        uses - the original extraction code assumed the wrapped shape and
        silently found nothing (real bug, caught by this live test)."""
        monkeypatch.setattr(odc_module.requests, "get", lambda *a, **k: _FakeResponse({"d": {"results": [
            {"OutboundDeliveryRequestItem": {
                "ObjectID": "ITEM1", "ParentObjectID": "PARENT1", "UUID": "U1",
                "OrderFulfilmentProcessingStatusCode": "1", "RayItemcode_KUT": "P-A",
                "RAYITEMDESCRIPTION_KUT": "desc A",
                "OutboundDeliveryRequestItemScheduleLine": [{"ObjectID": "SL1"}],
            }},
            {"OutboundDeliveryRequestItem": {
                "ObjectID": "ITEM2", "ParentObjectID": "PARENT1", "UUID": "U2",
                "OrderFulfilmentProcessingStatusCode": "1", "RayItemcode_KUT": "P-B",
                "RAYITEMDESCRIPTION_KUT": "desc B",
                "OutboundDeliveryRequestItemScheduleLine": [],
            }},
        ]}}))
        client = SAPOutboundDeliveryClient("https://sap.example.test/odataoutboundemergent", "u", "p", "vhost")
        rows = client.find_delivery_request_items("aabbccdd-0000-0000-0000-000000000000")
        assert rows[0]["schedule_line_object_id"] == "SL1"
        assert rows[1]["schedule_line_object_id"] is None

    def test_nested_schedule_line_object_id_extracted_wrapped_results(self, monkeypatch):
        """Defensive coverage in case a different SAP tenant/version DOES
        wrap this nav property the usual OData v2 `{"results": [...]}` way."""
        monkeypatch.setattr(odc_module.requests, "get", lambda *a, **k: _FakeResponse({"d": {"results": [
            {"OutboundDeliveryRequestItem": {
                "ObjectID": "ITEM1", "ParentObjectID": "PARENT1", "UUID": "U1",
                "OrderFulfilmentProcessingStatusCode": "1", "RayItemcode_KUT": "P-A",
                "RAYITEMDESCRIPTION_KUT": "desc A",
                "OutboundDeliveryRequestItemScheduleLine": {"results": [{"ObjectID": "SL1"}]},
            }},
        ]}}))
        client = SAPOutboundDeliveryClient("https://sap.example.test/odataoutboundemergent", "u", "p", "vhost")
        rows = client.find_delivery_request_items("aabbccdd-0000-0000-0000-000000000000")
        assert rows[0]["schedule_line_object_id"] == "SL1"


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
        out = MockOutboundClient([items], delivery_objects_by_uuid={"AU0": ("OBJ-API", "P8D1-185"), "AU1": ("OBJ-API", "P8D1-185")})
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
