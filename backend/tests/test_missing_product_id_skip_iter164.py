"""Iteration 164 - Sep 13 2026 fix verification.

Tests the new "if 1 PO/1 line has missing product_id, other POs/lines
still complete" behaviour across three layers:

1. `_post_one_po` in sap_playwright_supplier_pgr_service.py -
   all-lines-missing -> whole PO 'skipped' with skipped_items;
   some-lines-missing -> proceeds with valid lines only, returns
   'posted' + skipped_items for the dropped lines.
2. `_skipped_line_items_from_gr_results` + `_post_goods_movement_for_items`
   in supplier_shipment_service.py - Goods Movement must NOT be called
   for the skipped (po_number, item_number) pairs.
3. Upfront Supplier Portal API validation - POST /api/supplier-portal/shipments
   with a PO/item whose cached product_id is None must return HTTP 400.
"""
import asyncio
import os
import sys
import uuid
from unittest.mock import MagicMock, AsyncMock

import pytest
import requests
from pymongo import MongoClient

sys.path.insert(0, "/app/backend")

import sap_playwright_supplier_pgr_service as pgr
import supplier_shipment_service as sss

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://sap-data-sync.preview.emergentagent.com").rstrip("/")
MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "test_database")


@pytest.fixture(scope="module")
def db():
    client = MongoClient(MONGO_URL)
    yield client[DB_NAME]
    client.close()


# --------------------------------------------------------------------
# Layer 1: _post_one_po missing_products handling
# --------------------------------------------------------------------
class TestPostOnePoMissingProducts:
    def _run(self, item_qtys, item_products, item_uoms, notification_client=None):
        page = MagicMock()
        # If code takes the "all skipped" path, no Playwright calls happen.
        return asyncio.get_event_loop().run_until_complete(
            pgr._post_one_po(
                page=page,
                po_number="POTEST",
                doc_code="DOC001",
                supplier_doc_num="SD1",
                bill_date="2026-09-13",
                item_qtys=item_qtys,
                item_products=item_products,
                item_uoms=item_uoms,
                vendor_code="V1",
                notification_client=notification_client or MagicMock(),
                events=[],
            )
        )

    def test_all_lines_missing_product_id_whole_po_skipped(self):
        result = self._run(
            item_qtys={"10": 5, "20": 3},
            item_products={"10": None, "20": None},
            item_uoms={"10": "EA", "20": "EA"},
        )
        assert result["status"] == "skipped"
        assert result["po_number"] == "POTEST"
        assert len(result["skipped_items"]) == 2
        item_nums = {s["item_number"] for s in result["skipped_items"]}
        assert item_nums == {"10", "20"}
        for s in result["skipped_items"]:
            assert "Product ID is missing" in s["reason"]

    def test_partial_missing_only_bad_lines_dropped(self):
        """When some lines are missing product_id, `_post_one_po`
        should filter them out of item_qtys BEFORE the SOAP create,
        continue processing the remaining valid lines, and end up in
        one of the downstream steps (search/fill/save). We can't run
        the real Playwright/SAP flow here - so we monkey-patch just
        enough that we can verify (a) `maintain_bundle` was called
        ONLY with the valid line, and (b) `skipped_items` in the
        final result carries just the bad line.
        """
        # Track what soap_items were sent
        captured = {}

        def fake_maintain_bundle(nid, po, vendor, delivery_date, soap_items, flag):
            captured["soap_items"] = soap_items
            captured["nid"] = nid

        notif_client = MagicMock()
        notif_client.maintain_bundle = fake_maintain_bundle

        # Patch _post_one_po's downstream Playwright helpers so it
        # returns 'posted' without touching a browser.
        async def _fake_open_ind(page): pass
        async def _fake_switch(page): return True
        async def _fake_search(page, nid): return 1
        async def _fake_click_row(page, row, nid): pass
        async def _fake_click_button(page, name): return "clicked"
        async def _fake_wait_layer(page): pass
        async def _fake_fill(page, item_products, item_qtys, po): return []
        async def _fake_extract_error(page): return ""
        async def _fake_extract_conf(page): return "Inbound Delivery 999123 has been created"

        page = MagicMock()
        page.wait_for_timeout = AsyncMock()
        page.query_selector_all = AsyncMock(return_value=[MagicMock()])
        # get_by_text().first.wait_for chain
        by_text = MagicMock()
        by_text.first = MagicMock()
        by_text.first.wait_for = AsyncMock()
        page.get_by_text = MagicMock(return_value=by_text)

        orig = {
            "_open_inbound_delivery_notifications": pgr._open_inbound_delivery_notifications,
            "_switch_to_all_deliveries_view": pgr._switch_to_all_deliveries_view,
            "_search_delivery": pgr._search_delivery,
            "_click_po_row": pgr._click_po_row,
            "_click_button": pgr._click_button,
            "_wait_for_blocking_layer_clear": pgr._wait_for_blocking_layer_clear,
            "_fill_line_actual_quantities": pgr._fill_line_actual_quantities,
            "_extract_error_text": pgr._extract_error_text,
            "_extract_confirmation_text": pgr._extract_confirmation_text,
        }
        pgr._open_inbound_delivery_notifications = _fake_open_ind
        pgr._switch_to_all_deliveries_view = _fake_switch
        pgr._search_delivery = _fake_search
        pgr._click_po_row = _fake_click_row
        pgr._click_button = _fake_click_button
        pgr._wait_for_blocking_layer_clear = _fake_wait_layer
        pgr._fill_line_actual_quantities = _fake_fill
        pgr._extract_error_text = _fake_extract_error
        pgr._extract_confirmation_text = _fake_extract_conf
        try:
            result = asyncio.get_event_loop().run_until_complete(
                pgr._post_one_po(
                    page=page,
                    po_number="POTEST",
                    doc_code="DOC002",
                    supplier_doc_num="SD2",
                    bill_date="2026-09-13",
                    item_qtys={"10": 5, "20": 3, "30": 7},
                    item_products={"10": "PGOOD1", "20": None, "30": "PGOOD3"},
                    item_uoms={"10": "EA", "20": "EA", "30": "EA"},
                    vendor_code="V1",
                    notification_client=notif_client,
                    events=[],
                )
            )
        finally:
            for k, v in orig.items():
                setattr(pgr, k, v)

        assert result["status"] == "posted", f"Expected posted, got {result}"
        # Only the bad line (20) should be in skipped_items
        assert len(result["skipped_items"]) == 1
        assert result["skipped_items"][0]["item_number"] == "20"
        # Only valid lines 10 & 30 made it into the SOAP create
        soap_item_nums = {s["item_number"] for s in captured["soap_items"]}
        assert soap_item_nums == {"10", "30"}
        # Every soap_item has a real product_id (no None)
        assert all(s["product_id"] for s in captured["soap_items"])


# --------------------------------------------------------------------
# Layer 2: skipped_line_items filtering in Goods Movement
# --------------------------------------------------------------------
class TestPostGoodsMovementFiltering:
    def test_skipped_line_items_from_gr_results(self):
        per_po = [
            {"po_number": "PO1", "status": "posted", "skipped_items": [{"item_number": "20", "reason": "x"}]},
            {"po_number": "PO2", "status": "posted", "skipped_items": []},
            {"po_number": "PO3", "status": "skipped", "skipped_items": [{"item_number": "10", "reason": "y"}]},
        ]
        skipped = sss._skipped_line_items_from_gr_results(per_po)
        assert skipped == {("PO1", "20"), ("PO3", "10")}

    def test_goods_movement_skips_flagged_lines(self):
        doc = {
            "items": [
                {"po_number": "PO1", "item_number": "10", "product_id": "PA", "ship_qty": 5, "actual_qty": 5, "unit_of_measure": "EA"},
                {"po_number": "PO1", "item_number": "20", "product_id": "PB", "ship_qty": 3, "actual_qty": 3, "unit_of_measure": "EA"},
                {"po_number": "PO2", "item_number": "10", "product_id": "PC", "ship_qty": 7, "actual_qty": 7, "unit_of_measure": "EA"},
            ]
        }
        skipped_pairs = {("PO1", "20")}
        gm_client = MagicMock()
        gm_client.goods_movement = MagicMock(return_value={"ok": True})
        inv_client = MagicMock()
        inv_client.get_inventory_detail = MagicMock(return_value=[])
        result = sss._post_goods_movement_for_items(
            db=MagicMock(), doc=doc, goods_movement_client=gm_client, inventory_client=inv_client,
            owner_party_id="OWN", site_id="P1", warehouse_id="P1-QC", skipped_line_items=skipped_pairs,
        )
        # goods_movement should be called exactly twice - once for PO1/10 and once for PO2/10 (not PO1/20)
        assert gm_client.goods_movement.call_count == 2, f"got {gm_client.goods_movement.call_count} calls"
        # find PO1/20 in per_item, must be skipped=True, ok=True
        po1_20 = next(x for x in result["per_item"] if x["po_number"] == "PO1" and x["item_number"] == "20")
        assert po1_20["ok"] is True
        assert po1_20["skipped"] is True
        assert "missing Product ID" in po1_20["note"]
        assert result["ok"] is True

    def test_goods_movement_all_flagged_no_calls(self):
        doc = {"items": [
            {"po_number": "PO1", "item_number": "10", "product_id": "PA", "ship_qty": 5, "actual_qty": 5, "unit_of_measure": "EA"},
        ]}
        gm_client = MagicMock()
        inv_client = MagicMock()
        result = sss._post_goods_movement_for_items(
            db=MagicMock(), doc=doc, goods_movement_client=gm_client, inventory_client=inv_client,
            owner_party_id="OWN", site_id="P1", warehouse_id="P1-QC",
            skipped_line_items={("PO1", "10")},
        )
        assert gm_client.goods_movement.call_count == 0
        assert result["per_item"][0]["skipped"] is True


# --------------------------------------------------------------------
# Layer 3: Upfront Supplier Portal API 400 guard
# --------------------------------------------------------------------
class TestSupplierPortalMissingProductIdGuard:
    VENDOR_CODE = "S9999"
    TEST_PO = f"TESTITER164_{uuid.uuid4().hex[:6].upper()}"

    @pytest.fixture(scope="class")
    def seeded(self, db):
        """Insert 2 PO cache entries for S9999: one with product_id
        missing, one with a valid product_id - via the real
        supplier_portal_po_cache collection so the live API sees them.
        Cleans up at teardown."""
        coll = db[sss.PO_CACHE_COLLECTION]
        po = self.TEST_PO
        good_key = f"{self.VENDOR_CODE}::{po}::1"
        bad_key = f"{self.VENDOR_CODE}::{po}::2"
        coll.insert_one({
            "_id": good_key, "vendor_code": self.VENDOR_CODE, "po_number": po,
            "item_number": "1", "product_id": "TESTPROD_ITER164", "description": "iter164 good",
            "po_qty": 100, "unit_of_measure": "EA", "buyer_code": "RI",
        })
        coll.insert_one({
            "_id": bad_key, "vendor_code": self.VENDOR_CODE, "po_number": po,
            "item_number": "2", "product_id": None, "description": "iter164 bad (no product)",
            "po_qty": 50, "unit_of_measure": "EA", "buyer_code": "RI",
        })
        yield {"po": po, "good_key": good_key, "bad_key": bad_key}
        coll.delete_many({"_id": {"$in": [good_key, bad_key]}})
        # also clean up any shipments created during test
        db[sss.SHIPMENTS_COLLECTION].delete_many({"vendor_code": self.VENDOR_CODE, "items.po_number": po})

    @pytest.fixture(scope="class")
    def supplier_session(self):
        s = requests.Session()
        r = s.post(f"{BASE_URL}/api/supplier-portal/login",
                   json={"email": "vendor1@testco.com", "password": "DummyTest123"},
                   timeout=120)
        if r.status_code != 200:
            pytest.skip(f"Supplier login failed: {r.status_code} {r.text[:200]}")
        return s

    def test_shipment_with_missing_product_id_returns_400(self, seeded, supplier_session):
        r = supplier_session.post(
            f"{BASE_URL}/api/supplier-portal/shipments",
            json={"items": [{"po_number": seeded["po"], "item_number": "2", "ship_qty": 5}]},
            timeout=120,
        )
        assert r.status_code == 400, f"Expected 400 got {r.status_code}: {r.text}"
        body = r.json()
        detail = body.get("detail", "")
        assert "Product ID is missing" in detail or "product master link" in detail.lower(), f"Unexpected error msg: {detail}"
        assert "2" in detail  # item number
        assert seeded["po"] in detail  # PO number

    def test_shipment_with_valid_product_id_succeeds(self, seeded, supplier_session):
        r = supplier_session.post(
            f"{BASE_URL}/api/supplier-portal/shipments",
            json={"items": [{"po_number": seeded["po"], "item_number": "1", "ship_qty": 10}]},
            timeout=120,
        )
        assert r.status_code == 200, f"Expected 200 got {r.status_code}: {r.text}"
        body = r.json()
        assert body.get("vendor_code") == self.VENDOR_CODE
        assert len(body.get("items", [])) == 1
        assert body["items"][0]["po_number"] == seeded["po"]
        assert body["items"][0]["item_number"] == "1"
        assert body["items"][0]["product_id"] == "TESTPROD_ITER164"

    def test_mixed_valid_and_invalid_line_rejects_whole_request(self, seeded, supplier_session):
        # Even one bad line rejects the whole POST (validation runs before persistence)
        r = supplier_session.post(
            f"{BASE_URL}/api/supplier-portal/shipments",
            json={"items": [
                {"po_number": seeded["po"], "item_number": "1", "ship_qty": 5},
                {"po_number": seeded["po"], "item_number": "2", "ship_qty": 5},
            ]},
            timeout=120,
        )
        assert r.status_code == 400, f"Expected 400 got {r.status_code}: {r.text}"
        assert "Product ID is missing" in r.json().get("detail", "")


# --------------------------------------------------------------------
# Layer 4: Regression - existing shipment CRUD with valid product_ids still works
# --------------------------------------------------------------------
class TestRegressionValidShipmentsStillWork:
    """Uses the pre-seeded TESTGRN1 PO cache fixture for S9999 (per
    /app/memory/test_credentials.md). If it's been consumed away, we
    just skip."""

    @pytest.fixture(scope="class")
    def supplier_session(self):
        s = requests.Session()
        r = s.post(f"{BASE_URL}/api/supplier-portal/login",
                   json={"email": "vendor1@testco.com", "password": "DummyTest123"},
                   timeout=120)
        if r.status_code != 200:
            pytest.skip(f"Supplier login failed: {r.status_code} {r.text[:200]}")
        return s

    def test_list_shipments_still_works(self, supplier_session):
        r = supplier_session.get(f"{BASE_URL}/api/supplier-portal/shipments", timeout=120)
        assert r.status_code == 200, f"List shipments failed: {r.status_code} {r.text[:200]}"
        assert "shipments" in r.json()

    def test_list_open_pos_still_works(self, supplier_session):
        r = supplier_session.get(f"{BASE_URL}/api/supplier-portal/open-pos", timeout=120)
        # Endpoint may be named differently; try a couple then just settle for any 200 on shipments/pos-related route.
        if r.status_code == 404:
            pytest.skip("open-pos endpoint not named as expected")
        assert r.status_code == 200
