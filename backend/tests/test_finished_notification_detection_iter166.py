"""Iteration 166 - Sep 14 2026 fix verification.

Continuation of Issue 1 from the previous session: when SAP's "Post
Goods Receipt" button is disabled because the notification was already
fully received in SAP on a previous attempt, the app must detect this
via `_open_notification_detail` (open the notification's own detail
screen) whenever the list row's own visible columns don't carry a
"Finished" status text at all - instead of failing every retry forever
with a confusing "Post Goods Receipt is disabled" error.

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


class TestOpenNotificationDetail:
    def _make_row(self, link_text: str = None):
        row = MagicMock()
        cell = MagicMock()
        cell.query_selector_all = AsyncMock()
        if link_text is not None:
            link = MagicMock()
            link.inner_text = AsyncMock(return_value=link_text)
            link.click = AsyncMock()
            cell.query_selector = AsyncMock(return_value=link)
        else:
            cell.query_selector = AsyncMock(return_value=None)
        row.query_selector_all = AsyncMock(return_value=[cell])
        return row, cell

    def test_finds_and_clicks_matching_id_link(self):
        row, cell = self._make_row(link_text="NOTIF123-DOC001-29482")
        page = MagicMock()
        page.wait_for_timeout = AsyncMock()
        orig_wait = pgr._wait_for_blocking_layer_clear
        pgr._wait_for_blocking_layer_clear = AsyncMock()
        try:
            result = _run(pgr._open_notification_detail(page, row, "NOTIF123-DOC001-29482"))
        finally:
            pgr._wait_for_blocking_layer_clear = orig_wait
        assert result is True
        link = _run(cell.query_selector())
        link.click.assert_awaited_once_with(force=True)

    def test_no_matching_link_returns_false(self):
        row, cell = self._make_row(link_text=None)
        page = MagicMock()
        page.wait_for_timeout = AsyncMock()
        result = _run(pgr._open_notification_detail(page, row, "NOTIF123-DOC001-29482"))
        assert result is False


class TestPostOnePoDisabledButtonHandling:
    """Drives `_post_one_po` all the way to the "disabled" branch via
    monkeypatched Playwright helpers (same technique as iteration_164's
    test_partial_missing_only_bad_lines_dropped), then verifies the new
    detail-screen fallback correctly distinguishes a genuinely-Finished
    notification from a truly stuck one."""

    def _run_to_disabled_branch(self, row_text: str, detail_opens: bool, detail_text: str):
        page = MagicMock()
        page.wait_for_timeout = AsyncMock()
        page.query_selector_all = AsyncMock(return_value=[self._fake_row(row_text)])
        page.inner_text = AsyncMock(return_value=detail_text)
        by_text = MagicMock()
        by_text.first = MagicMock()
        by_text.first.wait_for = AsyncMock()
        page.get_by_text = MagicMock(return_value=by_text)
        page.screenshot = AsyncMock(return_value=b"fake")

        orig = {
            "_open_inbound_delivery_notifications": pgr._open_inbound_delivery_notifications,
            "_switch_to_all_deliveries_view": pgr._switch_to_all_deliveries_view,
            "_search_delivery": pgr._search_delivery,
            "_click_po_row": pgr._click_po_row,
            "_click_button": pgr._click_button,
            "_open_notification_detail": pgr._open_notification_detail,
        }
        pgr._open_inbound_delivery_notifications = AsyncMock()
        pgr._switch_to_all_deliveries_view = AsyncMock(return_value=True)
        pgr._search_delivery = AsyncMock(return_value=1)
        pgr._click_po_row = AsyncMock()
        pgr._click_button = AsyncMock(return_value="disabled")
        detail_mock = AsyncMock(return_value=detail_opens)
        pgr._open_notification_detail = detail_mock
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
        return result, detail_mock

    def _fake_row(self, text: str):
        row = MagicMock()
        row.inner_text = AsyncMock(return_value=text)
        return row

    def test_list_row_already_shows_finished_no_detail_navigation_needed(self):
        result, detail_mock = self._run_to_disabled_branch(row_text="NOTIF123 ... Finished", detail_opens=False, detail_text="")
        assert result["status"] == "posted"
        detail_mock.assert_not_called()

    def test_list_row_missing_status_detail_screen_confirms_finished(self):
        result, detail_mock = self._run_to_disabled_branch(
            row_text="NOTIF123  29482  V1", detail_opens=True, detail_text="Delivery Status: Finished",
        )
        assert result["status"] == "posted"
        detail_mock.assert_called_once()

    def test_neither_row_nor_detail_show_finished_fails_cleanly(self):
        result, _ = self._run_to_disabled_branch(
            row_text="NOTIF123  29482  V1", detail_opens=True, detail_text="Delivery Status: Not Started",
        )
        assert result["status"] == "failed"
        assert result["failed_step"] == "opening_receipt"

    def test_detail_screen_unreachable_falls_back_to_failure(self):
        result, _ = self._run_to_disabled_branch(
            row_text="NOTIF123  29482  V1", detail_opens=False, detail_text="",
        )
        assert result["status"] == "failed"
