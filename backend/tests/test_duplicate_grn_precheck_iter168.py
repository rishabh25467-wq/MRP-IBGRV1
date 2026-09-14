"""Iteration 168 - Aug 2026 fix verification.

Real incident: shipment DSWRPG (PO 29482) accumulated 24 duplicate
Inbound Delivery Notification documents in SAP over repeated retries,
because SAP never rejects a duplicate DeliveryNotificationID create -
2 of them (53015, 53018) were independently confirmed "Finished" by
the user directly in SAP, i.e. a real double Goods Receipt. Fixes
`_post_one_po` to query SAP's own confirmation report FIRST and skip
re-creating/re-posting when this exact notification_id+PO already has
a real confirmed Goods Receipt.
"""
import asyncio
import sys
sys.path.insert(0, "/app/backend")
import sap_playwright_supplier_pgr_service as svc


class _FakeReportClientFound:
    def find_confirmation_rows(self, po_number, notification_id):
        assert po_number == "29482"
        assert notification_id == svc._build_notification_id("TEST_07/27/PM", "DSWRPG", "29482")
        return [{"CDELIVERY_UUID": "53015", "CREF_ID": notification_id}]


class _FakeReportClientNotFound:
    def find_confirmation_rows(self, po_number, notification_id):
        return []


class _FakeNotificationClientShouldNotBeCalled:
    def maintain_bundle(self, *a, **k):
        raise AssertionError("maintain_bundle must NOT be called when SAP already shows a confirmed receipt")


def test_skips_create_and_returns_posted_when_already_confirmed():
    result = asyncio.run(svc._post_one_po(
        page=None, po_number="29482", doc_code="DSWRPG", supplier_doc_num="TEST_07/27/PM",
        bill_date="2026-09-13", item_qtys={"1": 3}, item_products={"1": "WAS8PZ"}, item_uoms={"1": "EA"},
        vendor_code="RAD-P2-S", notification_client=_FakeNotificationClientShouldNotBeCalled(),
        confirmation_report_client=_FakeReportClientFound(),
    ))
    assert result["status"] == "posted"
    assert result["inbound_delivery_id"] == "53015"
    assert any("already has a confirmed Goods Receipt" in e for e in result["events"])


def test_precheck_failure_does_not_block_normal_flow():
    class _FakeReportClientRaises:
        def find_confirmation_rows(self, po_number, notification_id):
            raise Exception("SAP timeout")

    class _FakeNotificationClientRecordsCall:
        called = False

        def maintain_bundle(self, *a, **k):
            self.called = True
            raise Exception("stop here - only checking the pre-check didn't block this call")

    nc = _FakeNotificationClientRecordsCall()
    try:
        asyncio.run(svc._post_one_po(
            page=None, po_number="29482", doc_code="DSWRPG", supplier_doc_num="TEST_07/27/PM",
            bill_date="2026-09-13", item_qtys={"1": 3}, item_products={"1": "WAS8PZ"}, item_uoms={"1": "EA"},
            vendor_code="RAD-P2-S", notification_client=nc,
            confirmation_report_client=_FakeReportClientRaises(),
        ))
    except Exception:
        pass
    assert nc.called, "a pre-check error should not prevent the normal create flow from running"
