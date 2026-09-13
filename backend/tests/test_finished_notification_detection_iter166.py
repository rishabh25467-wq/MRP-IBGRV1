"""Iteration 166 - Sep 14 2026 fix verification (CORRECTED).

Continuation of Issue 1 from the previous session. The FIRST attempt at
this fix (clicking a notification ID link to "navigate into" a detail
screen) was proven wrong by the user's own live screenshot of real PO
29482/notification TEST_0506-WFRRSL-29482: there is no such link in the
list row at all. The real gap is that "Delivery Status" is never one of
the list's own columns - it only ever renders in the "Details: Delivery
Notification ..." panel SAP's own master-detail layout shows BELOW the
list the instant a row is selected (already done by `_click_po_row`
before this check runs) - so the fix just needs to read the WHOLE page's
text, not just the row's own text, no extra navigation needed.

No live SAP sandbox exists to dry-run Playwright DOM interactions
against - all Playwright objects here are mocked. Real confirmation
still needs a live GRN retry against an already-Finished PO.
"""
import asyncio
import sys
from unittest.mock import MagicMock, AsyncMock

sys.path.insert(0, "/app/backend")

import sap_playwright_supplier_pgr_service as pgr


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestIsNotificationFinishedInDetailsPanel:
    def _page_with_text(self, body_text: str):
        page = MagicMock()
        page.inner_text = AsyncMock(return_value=body_text)
        return page

    def test_finished_on_same_line_as_label(self):
        page = self._page_with_text("Some header\nDelivery Status: Finished\nOther field")
        assert _run(pgr._is_notification_finished_in_details_panel(page)) is True

    def test_finished_on_next_line_stacked_layout(self):
        page = self._page_with_text("Delivery Status\nFinished\nCancellation Status\nNot Canceled")
        assert _run(pgr._is_notification_finished_in_details_panel(page)) is True

    def test_not_started_returns_false(self):
        page = self._page_with_text("Delivery Status: Not Started\nRelease Status: Released")
        assert _run(pgr._is_notification_finished_in_details_panel(page)) is False

    def test_no_delivery_status_line_at_all_returns_false(self):
        page = self._page_with_text("Some unrelated page\nFinished appears elsewhere in a dropdown option")
        assert _run(pgr._is_notification_finished_in_details_panel(page)) is False

    def test_no_false_positive_from_unrelated_finished_text(self):
        """A status FILTER dropdown's hidden option list can genuinely
        contain the word "Finished" elsewhere on the page - must not
        count unless it's actually on/next-to the Delivery Status line."""
        page = self._page_with_text("Filter: Advised, Received, Finished, Canceled\nDelivery Status: Not Started")
        assert _run(pgr._is_notification_finished_in_details_panel(page)) is False


class TestPostOnePoDisabledButtonHandling:
    """Drives `_post_one_po` all the way to the "disabled" branch via
    monkeypatched Playwright helpers (same technique as iteration_164's
    test_partial_missing_only_bad_lines_dropped)."""

    def _run_to_disabled_branch(self, row_text: str, page_body_text: str):
        page = MagicMock()
        page.wait_for_timeout = AsyncMock()
        page.query_selector_all = AsyncMock(return_value=[self._fake_row(row_text)])
        page.inner_text = AsyncMock(return_value=page_body_text)
        page.screenshot = AsyncMock(return_value=b"fake")
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
        }
        pgr._open_inbound_delivery_notifications = AsyncMock()
        pgr._switch_to_all_deliveries_view = AsyncMock(return_value=True)
        pgr._search_delivery = AsyncMock(return_value=1)
        pgr._click_po_row = AsyncMock()
        pgr._click_button = AsyncMock(return_value="disabled")
        notif_client = MagicMock()
        notif_client.maintain_bundle = MagicMock()
        try:
            result = _run(pgr._post_one_po(
                page=page, po_number="POTEST", doc_code="DOC001", supplier_doc_num="SD1",
                bill_date="2026-09-14", item_qtys={"10": 5}, item_products={"10": "PGOOD1"},
                item_uoms={"10": "EA"}, vendor_code="V1", notification_client=notif_client, events=[],
            ))
        finally:
            for k, v in orig.items():
                setattr(pgr, k, v)
        return result

    def _fake_row(self, text: str):
        row = MagicMock()
        row.inner_text = AsyncMock(return_value=text)
        return row

    def test_real_incident_repro_details_panel_shows_finished(self):
        """Exact repro of the user's real report: PO 29482/notification
        TEST_0506-WFRRSL-29482 - list row has no "Finished" text at all
        (no such column shown), but the Details panel below it (already
        rendered from row selection, no click needed) does."""
        result = self._run_to_disabled_branch(
            row_text="TEST_0506-WFRRSL-29482  Received  Released  13.09.2026  Radish Technologies ALIGARH P2  Technical User  Supplier Delivery",
            page_body_text=(
                "Inbound Delivery Notifications\n"
                "Details: Delivery Notification TEST_0506-WFRRSL-29482\n"
                "Status\n"
                "Consistency Status: Consistent\n"
                "Delivery Notification Status: Received\n"
                "Release Status: Released\n"
                "Delivery Status: Finished\n"
                "Cancellation Status: Not Canceled\n"
            ),
        )
        assert result["status"] == "posted", f"Expected posted, got {result}"

    def test_genuinely_stuck_document_still_fails(self):
        result = self._run_to_disabled_branch(
            row_text="NOTIF123  29482  V1",
            page_body_text="Details: Delivery Notification NOTIF123\nDelivery Status: Not Started\n",
        )
        assert result["status"] == "failed"
        assert result["failed_step"] == "opening_receipt"
