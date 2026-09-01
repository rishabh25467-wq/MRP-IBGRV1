"""Supplier Portal GRN - live Goods Receipt for external supplier POs via
SAP UI automation (Aug 29 2026).

Root cause this replaces: `sap_gsa_write_client.post_goods_receipt`
(the API-based GSA write) only works for non-stock/service PO lines -
confirmed dead end for real stock materials (see
supplier_shipment_service.py module docstring history). The user's own
ops team showed the manual screen that actually works for real stock:

    Inbound Logistics work center -> Purchase Orders view -> search the
    exact PO -> select its row -> "Post Goods Receipt" button -> opens a
    "Create Inbound Delivery and Goods Receipt" dialog -> fill Delivery
    Notification ID (-> supplier_doc_num, entered by staff) + Actual
    Delivery Date (-> the supplier's Bill Date, ALSO entered by staff,
    despite auto-filling with "now" by default) + Actual Quantity per
    line (-> staff-confirmed received qty, NOT the vendor's own claimed
    ship_qty - user's explicit ask) -> Save and Close.

Scope (user's explicit ask, Aug 29 2026): Goods Receipt only. Invoice
creation (a separate SAP screen, Supplier Invoicing work center) is a
later phase, done by a different user - never touched here.

UNVERIFIED end-to-end (no sandbox/dry-run exists for this - every SAP
write here is real and irreversible). Read-only navigate+search+fill was
verified live up to (never including) the final Save and Close click -
see /app/backend/playwright_debug/grn_investigation/ screenshots from
that investigation. The exact "Actual Delivery Date" input format and
the exact Actual Quantity grid column layout are BEST-EFFORT from the
user's screenshots only, not live-confirmed - the very first real call
into post_goods_receipt_via_ui MUST be supervised/reviewed by the user
before this is trusted for unattended use."""
import logging
import os
from datetime import datetime, timezone

from sap_playwright_pgr_service import _login, _wait_for_blocking_layer_clear, _extract_error_text, _humanize_error, _click_button

logger = logging.getLogger(__name__)
DEBUG_SCREENSHOT_DIR = "/app/backend/playwright_debug/supplier_pgr_failures"


async def _open_purchase_orders(page) -> None:
    """Inbound Logistics work center -> Purchase Orders view (confirmed
    live during the Aug 29 2026 read-only investigation - distinct from
    _open_inbound_delivery_notifications in sap_playwright_pgr_service.py,
    which is the STO/internal-transfer screen, not this one)."""
    await _wait_for_blocking_layer_clear(page)
    await page.locator('[aria-label="Inbound Logistics"]').first.click(force=True)
    await page.wait_for_timeout(2500)
    po_link = page.get_by_text("Purchase Orders", exact=True)
    for i in range(await po_link.count()):
        el = po_link.nth(i)
        if await el.is_visible():
            await el.click(force=True)
            await page.wait_for_timeout(3000)
            await _wait_for_table_load(page)
            return
    raise RuntimeError("Could not find the 'Purchase Orders' view under Inbound Logistics")


async def _wait_for_table_load(page, timeout: int = 60000) -> None:
    """The Purchase Orders list shows its own inline "Loading..." text
    while fetching rows (distinct from `_wait_for_blocking_layer_clear`'s
    full-page modal overlay) - toolbar buttons (incl. the Filter icon)
    stay disabled/unclickable the whole time. Root cause of the very
    first live supervised test (Sep 1 2026) failing with "Could not find
    the 'Purchase Order ID' filter field" on 2/2 real POs: the fixed
    2500/3000ms waits were shorter than this tenant's actual load time,
    so the Filter click landed while the list was still loading."""
    try:
        await page.get_by_text("Loading...", exact=True).first.wait_for(state="hidden", timeout=timeout)
    except Exception:
        pass
    await page.wait_for_timeout(500)


async def _search_po_exact(page, po_number: str) -> int:
    """Exact "Purchase Order ID" filter (NOT the free-text search box -
    confirmed live it can return 0 hits for a real, valid PO number
    depending on which saved-query preset is active). Switches the base
    view to "All Purchase Orders by Selection" first (broadest possible,
    no hidden status/date scoping) then fills the Filter panel's own
    "Purchase Order ID" field. Returns the row count found."""
    base_dd = page.locator("text=Open Purchase Orders").first
    if await base_dd.count() > 0:
        await base_dd.click(force=True)
        await page.wait_for_timeout(1000)
        broad_opt = page.get_by_text("All Purchase Orders by Selection", exact=True).first
        if await broad_opt.count() > 0:
            await broad_opt.click(force=True)
            await page.wait_for_timeout(1500)
            await _wait_for_table_load(page)

    filter_btn = None
    for b in await page.query_selector_all(".sapMBtnIconLeft, .sapMBtnBase"):
        if not await b.is_visible():
            continue
        aria = (await b.get_attribute("aria-label")) or ""
        title = (await b.get_attribute("title")) or ""
        if "filter" in (aria + title).lower():
            filter_btn = b
            break
    if filter_btn is None:
        raise RuntimeError("Could not find the Filter icon on the Purchase Orders screen")
    await filter_btn.click(force=True)
    await page.wait_for_timeout(1500)

    po_id_input = None
    for lbl in await page.query_selector_all("label"):
        if (await lbl.inner_text()).strip() == "Purchase Order ID":
            for_id = await lbl.get_attribute("for")
            if for_id:
                po_id_input = await page.query_selector(f"#{for_id}")
            break
    if po_id_input is None:
        raise RuntimeError("Could not find the 'Purchase Order ID' filter field")
    await po_id_input.fill(po_number)
    go_btn = page.get_by_text("Go", exact=True).first
    if await go_btn.count() > 0:
        await go_btn.click(force=True)
        await page.wait_for_timeout(4000)
    return len(await page.query_selector_all('tr[id^="__table"]'))


async def _fill_line_actual_quantities(page, item_products: dict, item_qtys: dict) -> list:
    """item_products: {item_number: product_id}, item_qtys: {item_number:
    qty}. Matches each grid row to a PO line by its Product ID TEXT,
    never by row order (a real, confirmed-dangerous bug found by
    testing_agent code review, Aug 29 2026: order-based matching could
    silently post a quantity against the WRONG line on a real PO if the
    dialog's own row order ever differs from the shipment's item order).
    Scoped to the #sap-ui-static popup container when it exists and has
    rows (SAPUI5's usual place to render dialogs/popovers) - falls back
    to the whole page only if that container is empty, so this can never
    accidentally match a <tr> from the background Purchase Orders list
    instead of the actual dialog. Returns the item_numbers it could NOT
    fill - caller MUST treat any non-empty result as a hard failure,
    never silently post a wrong/blank/zero quantity."""
    scope = page.locator("#sap-ui-static")
    if await scope.count() == 0 or await scope.locator('tr[id^="__table"]').count() == 0:
        scope = page

    qty_col = -1
    for i, h in enumerate(await scope.locator("th").all()):
        if (await h.inner_text()).strip() == "Actual Quantity":
            qty_col = i
            break
    if qty_col == -1:
        return list(item_qtys.keys())

    rows = await scope.locator('tr[id^="__table"]').all()
    unfilled = []
    for item_number, qty in item_qtys.items():
        product_id = item_products.get(item_number)
        matched_row = None
        for row in rows:
            row_text = await row.inner_text()
            if product_id and product_id in row_text:
                matched_row = row
                break
        if matched_row is None:
            unfilled.append(item_number)
            continue
        cells = await matched_row.query_selector_all("td")
        if len(cells) <= qty_col:
            unfilled.append(item_number)
            continue
        qty_input = await cells[qty_col].query_selector("input")
        if not qty_input:
            unfilled.append(item_number)
            continue
        await qty_input.fill("")
        await qty_input.fill(str(qty))
        await qty_input.press("Tab")
    return unfilled


async def _post_one_po(page, po_number: str, supplier_doc_num: str, bill_date: str, item_qtys: dict, item_products: dict) -> dict:
    await _open_purchase_orders(page)
    hits = await _search_po_exact(page, po_number)
    if hits == 0:
        return {"po_number": po_number, "status": "skipped", "error": "PO not found in SAP's Purchase Orders view - check its status is actually released/receivable (not 'In Preparation')"}
    rows = await page.query_selector_all('tr[id^="__table"]')
    await rows[0].click(force=True)
    await page.wait_for_timeout(1500)

    click_result = await _click_button(page, "Post Goods Receipt")
    if click_result == "disabled":
        return {"po_number": po_number, "status": "failed", "error": "Post Goods Receipt is disabled for this PO in SAP"}
    if click_result == "not_found":
        return {"po_number": po_number, "status": "failed", "error": "Post Goods Receipt button not found for this PO"}
    await page.wait_for_timeout(4000)

    notif_input = None
    for lbl in await page.query_selector_all("label"):
        if "Delivery Notification ID" in (await lbl.inner_text()).strip():
            for_id = await lbl.get_attribute("for")
            if for_id:
                notif_input = await page.query_selector(f"#{for_id}")
            break
    if notif_input:
        await notif_input.fill(supplier_doc_num or "")

    if bill_date:
        date_input = None
        for lbl in await page.query_selector_all("label"):
            if "Actual Delivery Date" in (await lbl.inner_text()).strip():
                for_id = await lbl.get_attribute("for")
                if for_id:
                    date_input = await page.query_selector(f"#{for_id}")
                break
        if date_input:
            await date_input.fill(bill_date)
            await date_input.press("Tab")

    unfilled = await _fill_line_actual_quantities(page, item_products, item_qtys)
    if unfilled:
        await _click_button(page, "Close")
        return {"po_number": po_number, "status": "failed", "error": f"Could not enter Actual Quantity for item(s) {', '.join(unfilled)}"}

    if await _click_button(page, "Save and Close") != "clicked":
        await _click_button(page, "Close")
        return {"po_number": po_number, "status": "failed", "error": "Save and Close button not found"}
    await page.wait_for_timeout(6000)

    error_text = await _extract_error_text(page)
    if error_text:
        await _click_button(page, "Close")
        return {"po_number": po_number, "status": "failed", "error": _humanize_error(error_text)}
    return {"po_number": po_number, "status": "posted"}


async def post_goods_receipt_via_ui(po_items: dict, progress_cb=None) -> dict:
    """po_items: {po_number: {"supplier_doc_num": str, "bill_date": str,
    "item_qtys": {item_number: qty}, "item_products": {item_number:
    product_id}}} - one call per distinct PO, grouping every line item
    of that PO into a single submission (user's explicit ask, Aug 29
    2026), matching the manual flow exactly. `item_products` is required
    for the Actual Quantity grid to match the right row by Product ID
    (see _fill_line_actual_quantities' docstring for why row-order
    matching alone is not safe to trust).

    progress_cb(phase, current, total) - same contract as
    sap_playwright_pgr_service.post_goods_receipts_via_ui, wording never
    reveals SAP/Playwright to the end user.

    Returns {"results": [{"po_number", "status": "posted"|"failed"|
    "skipped", "error"?}]}."""
    from playwright.async_api import async_playwright
    import playwright_concurrency

    po_numbers = list(po_items.keys())
    total = len(po_numbers)
    results = []

    def _progress(phase: str, current: int = 0) -> None:
        if progress_cb:
            try:
                progress_cb(phase, current, total)
            except Exception:
                pass

    _progress("queued")
    username, password = await playwright_concurrency.acquire()
    try:
        async with async_playwright() as p:
            browser = await playwright_concurrency.launch_chromium(p)
            playwright_concurrency.register_browser(browser)
            try:
                page = await browser.new_page(viewport={"width": 1600, "height": 900})
                _progress("processing", 0)
                await _login(page, username, password)
                for idx, po_number in enumerate(po_numbers, start=1):
                    _progress("processing", idx - 1)
                    spec = po_items[po_number]
                    try:
                        result = await _post_one_po(
                            page, po_number, spec.get("supplier_doc_num"), spec.get("bill_date"),
                            spec.get("item_qtys") or {}, spec.get("item_products") or {},
                        )
                    except Exception as e:
                        logger.error(f"Playwright Supplier GRN failed for PO {po_number}: {e}")
                        try:
                            os.makedirs(DEBUG_SCREENSHOT_DIR, exist_ok=True)
                            await page.screenshot(path=f"{DEBUG_SCREENSHOT_DIR}/{po_number}.png")
                        except Exception:
                            pass
                        result = {"po_number": po_number, "status": "failed", "error": "Could not reach SAP's receipt screen - please retry"}
                    results.append(result)
                    if result["status"] == "failed" and idx < total:
                        try:
                            await page.close()
                        except Exception:
                            pass
                        page = await browser.new_page(viewport={"width": 1600, "height": 900})
                        await _login(page, username, password)
            finally:
                await browser.close()
                playwright_concurrency.unregister_browser(browser)
    finally:
        playwright_concurrency.release((username, password))

    _progress("done", total)
    return {"results": results, "completed_at": datetime.now(timezone.utc).isoformat()}
