"""Iteration 167 - Sep 14 2026 fix verification.

Real incident: shipment AB54TT on live production had 3 PO lines
(29510 real, 29533/29534 disposable test POs the agent later cancelled
in SAP for the Cancel PO feature's own live testing). 1 of 3 POs
posted, the other 2 came back "skipped" (Cancelled in SAP) - the old
finalize_goods_receipt logic only recognized ALL-posted or ALL-skipped
as terminal, so this mixed case fell through to "pending" (retried
forever) even though nothing could ever change. Fixed with a new
"partial" sap_sync_status. Uses this preview's own test DB directly
(no mocking of finalize_goods_receipt itself) with a throwaway shipment
doc, cleaned up after.
"""
import sys
sys.path.insert(0, "/app/backend")
import os
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")
from pymongo import MongoClient
import supplier_shipment_service as sss

db = MongoClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]

DOC_CODE = "TESTITER167AB"


def _make_doc():
    sss.db_insert_test_shipment = None  # no-op marker
    doc = {
        "_id": DOC_CODE.upper(), "doc_code": DOC_CODE, "status": "approved", "vendor_code": "V-TEST", "site_id": "P1",
        "warehouse_id": "P1-RM", "sap_gr_retry_count": 0,
        "items": [
            {"po_number": "29510", "item_number": "3", "product_id": "HRSHEET", "ship_qty": 10, "unit_of_measure": "KGM"},
            {"po_number": "29533", "item_number": "1", "product_id": "WAS8PZ", "ship_qty": 1, "unit_of_measure": "EA"},
            {"po_number": "29534", "item_number": "1", "product_id": "WAS8PZ", "ship_qty": 1, "unit_of_measure": "EA"},
        ],
    }
    db[sss.SHIPMENTS_COLLECTION].delete_one({"_id": DOC_CODE.upper()})
    db[sss.SHIPMENTS_COLLECTION].insert_one(doc)
    return doc


class _FakeMovementClient:
    def post_goods_movement(self, *a, **k):
        return {"ok": True, "movement_id": "FAKE-MVT-1"}


class _FakeInventoryClient:
    pass


def teardown_module(module):
    db[sss.SHIPMENTS_COLLECTION].delete_one({"_id": DOC_CODE.upper()})


class TestSkippedLineItemsFromGrResults:
    def test_whole_po_skip_excludes_all_its_lines(self):
        doc = _make_doc()
        gr_results = [
            {"po_number": "29510", "status": "posted"},
            {"po_number": "29533", "status": "skipped"},
            {"po_number": "29534", "status": "skipped"},
        ]
        excluded = sss._skipped_line_items_from_gr_results(doc, gr_results)
        assert ("29533", "1") in excluded
        assert ("29534", "1") in excluded
        assert ("29510", "3") not in excluded

    def test_partial_line_skip_within_posted_po_still_works(self):
        doc = _make_doc()
        gr_results = [{"po_number": "29510", "status": "posted", "skipped_items": [{"item_number": "3", "reason": "missing product_id"}]}]
        excluded = sss._skipped_line_items_from_gr_results(doc, gr_results)
        assert ("29510", "3") in excluded


class TestFinalizeGoodsReceiptPartialStatus:
    def test_ab54tt_repro_mixed_posted_and_skipped_becomes_partial(self):
        _make_doc()
        gr_results = [
            {"po_number": "29510", "status": "posted", "inbound_delivery_id": "IDN-1"},
            {"po_number": "29533", "status": "skipped", "error": "SAP rejected the Inbound Delivery Notification: Reference order item canceled"},
            {"po_number": "29534", "status": "skipped", "error": "SAP rejected the Inbound Delivery Notification: Reference order item canceled"},
        ]
        result = sss.finalize_goods_receipt(
            db, DOC_CODE, gr_results, _FakeMovementClient(), _FakeInventoryClient(),
            owner_party_id="OWNER1", sap_username="itadmin",
        )
        assert result["sap_sync_status"] == "partial", f"expected partial, got {result.get('sap_sync_status')}"
        assert result["sap_movement_status"] == "posted"

    def test_all_posted_still_posted_not_partial(self):
        _make_doc()
        gr_results = [{"po_number": "29510", "status": "posted", "inbound_delivery_id": "IDN-1"}]
        result = sss.finalize_goods_receipt(
            db, DOC_CODE, gr_results, _FakeMovementClient(), _FakeInventoryClient(),
            owner_party_id="OWNER1", sap_username="itadmin",
        )
        assert result["sap_sync_status"] == "posted"

    def test_all_skipped_stays_skipped_not_partial(self):
        _make_doc()
        gr_results = [
            {"po_number": "29533", "status": "skipped", "error": "cancelled"},
            {"po_number": "29534", "status": "skipped", "error": "cancelled"},
        ]
        result = sss.finalize_goods_receipt(
            db, DOC_CODE, gr_results, _FakeMovementClient(), _FakeInventoryClient(),
            owner_party_id="OWNER1", sap_username="itadmin",
        )
        assert result["sap_sync_status"] == "skipped"

    def test_partial_shipment_cannot_be_retried(self):
        _make_doc()
        gr_results = [
            {"po_number": "29510", "status": "posted"},
            {"po_number": "29533", "status": "skipped", "error": "cancelled"},
            {"po_number": "29534", "status": "skipped", "error": "cancelled"},
        ]
        sss.finalize_goods_receipt(db, DOC_CODE, gr_results, _FakeMovementClient(), _FakeInventoryClient(), owner_party_id="OWNER1", sap_username="itadmin")
        try:
            sss.prepare_retry_goods_receipt(db, DOC_CODE)
            assert False, "should have raised - nothing left to retry on a partial shipment"
        except sss.ShipmentValidationError:
            pass
