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
import re
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
    "Purchase Order ID" field. Returns the row count found.

    Sep 2 2026 BUG FOUND + FIXED (user report: PO 29086 genuinely
    Released in SAP but the GRN screen said "PO not found... check its
    status"): the base-view dropdown's CURRENTLY SELECTED label is not
    stable across logins/bot credential slots - it was hardcoded to
    look for the text "Open Purchase Orders" specifically, but this run
    landed on "Due and Overdue Purchase Orders" instead (a real, valid
    SAP-remembered variant, just a narrower one that silently excludes
    perfectly valid POs like 29086), so the locator matched 0 elements,
    the broaden-to-"All Purchase Orders by Selection" step never ran,
    and the PO ID filter then searched the WRONG, narrower scope - per
    user's explicit instruction ("do not search with due and overdue
    filter, search in all purchase orders"). FIRST attempted fix
    (`.sapMSlt` class) was ALSO wrong - live DOM inspection found 3
    `.sapMSlt` elements on this page (the global header's "All
    Categories" search-category selector is #0, this base-view select
    is #1, "Group By" is #2), so `.first` kept grabbing the unrelated
    header control. Real fix: `[id$="-defaultSetDDLB"]` - SAPUI5's own
    stable ID suffix for a list report's "default view set" dropdown,
    confirmed live to always resolve to the correct control regardless
    of its current label or how many other `.sapMSlt`s are on the page."""
    base_dd = page.locator('[id$="-defaultSetDDLB"]').first
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
        # Sep 2 2026 BUG FOUND + FIXED (same investigation as the
        # PO-search/row-select/loading-time fixes above, confirmed live
        # via backend logs: "'Locator' object has no attribute
        # 'query_selector_all'"): `rows` here are Playwright LOCATORS
        # (from `.locator(...).all()`), not ElementHandles - Locators
        # don't have `query_selector_all`/`query_selector` at all
        # (that's ElementHandle-only API). Must use `.locator("td")` +
        # `.count()`/`.nth()` instead, consistently.
        cells = matched_row.locator("td")
        cell_count = await cells.count()
        if cell_count <= qty_col:
            unfilled.append(item_number)
            continue
        # Sep 2 2026 fix: this cell actually holds TWO inputs (confirmed
        # live via a strict-mode Playwright error) - the real quantity
        # text field AND a separate unit-of-measure combobox rendered in
        # the same <td>. `:not([role="combobox"])` picks only the real
        # quantity field.
        qty_input = cells.nth(qty_col).locator('input:not([role="combobox"])')
        if await qty_input.count() == 0:
            unfilled.append(item_number)
            continue
        await qty_input.first.fill("")
        await qty_input.first.fill(str(qty))
        await qty_input.first.press("Tab")
    return unfilled


async def _click_po_row(page, row, po_number: str) -> None:
    """SELECTS (does not navigate into) the row so the list's own
    toolbar "Post Goods Receipt" button becomes enabled - matches the
    real manual flow ("search the exact PO -> select its row -> 'Post
    Goods Receipt' button", see module docstring). Sep 2 2026 BUG FOUND
    + FIXED (same investigation as the PO-search fix above), THREE
    attempts: (1) original `row.click()` landed at the row's bounding-
    box CENTER, which falls on the "Supplier Name" cell and navigates
    to that Business Partner's own detail screen instead of selecting
    the row; (2) clicking the first `<td>`'s link was wrong too - that
    cell has no link, it's an empty row-selection indicator column;
    (3) clicking the "Purchase Order ID" link cell (matched by its own
    text) navigates INTO the PO's own detail screen - confirmed live
    that screen has NO "Post Goods Receipt" button at all (it's a
    LIST-toolbar-only action, requires the row merely selected, not
    opened). Real fix: click the empty first `<td>` (the selection
    indicator column itself, no link) - this selects the row in place
    without navigating anywhere."""
    cells = await row.query_selector_all("td")
    if cells:
        await cells[0].click(force=True)
    else:
        await row.click(force=True)


async def _extract_confirmation_text(page) -> str:
    """Sep 2 2026 addition (user's ask: "show SAP inbound number for
    user's reference"): the SAME role='alert' region `_extract_error_text`
    reads from also carries the SUCCESS confirmation on a real save
    ("Inbound delivery 123456 has been created" / "Your entries have
    been saved") - `_extract_error_text` deliberately discards anything
    without "Error" in it. This returns that text regardless, so the
    caller can pull the real Inbound Delivery ID out of it."""
    for sel in ["[role='alert']", "[role='alertdialog']", ".sapMMessageToast"]:
        el = page.locator(sel).first
        if await el.count() > 0 and await el.is_visible():
            text = (await el.inner_text()).strip()
            if text:
                return text[:300]
    return ""


def _extract_inbound_delivery_id(confirmation_text: str) -> str:
    """Pulls the numeric Inbound Delivery ID out of SAP's own confirmation
    text, e.g. "Inbound Delivery 1801234 has been created"."""
    m = re.search(r"[Ii]nbound [Dd]elivery\s+(\d+)", confirmation_text or "")
    return m.group(1) if m else None


async def _post_one_po(page, po_number: str, supplier_doc_num: str, bill_date: str, item_qtys: dict, item_products: dict, on_step=None) -> dict:
    async def step(name):
        if on_step:
            on_step(name)

    await step("searching")
    await _open_purchase_orders(page)
    hits = await _search_po_exact(page, po_number)
    if hits == 0:
        return {"po_number": po_number, "status": "skipped", "error": "PO not found in SAP's Purchase Orders view - check its status is actually released/receivable (not 'In Preparation')"}
    rows = await page.query_selector_all('tr[id^="__table"]')
    await _click_po_row(page, rows[0], po_number)
    await page.wait_for_timeout(1500)

    click_result = await _click_button(page, "Post Goods Receipt")
    if click_result == "disabled":
        return {"po_number": po_number, "status": "failed", "error": "Post Goods Receipt is disabled for this PO in SAP"}
    if click_result == "not_found":
        return {"po_number": po_number, "status": "failed", "error": "Post Goods Receipt button not found for this PO"}
    await step("opening_receipt")
    # Sep 2 2026 fix (same investigation as the search/row-select fixes
    # above): the "Create Inbound Delivery and Goods Receipt" screen's
    # own Line Items grid shows an inline "Loading..." text while its
    # data fetches (same pattern as `_wait_for_table_load` on the list
    # screen) - a fixed 4000ms wait was shorter than this tenant's real
    # load time, so `_fill_line_actual_quantities` used to run against
    # an empty/still-loading grid and report every item "could not be
    # filled". Also waits for the full-page blocking overlay to clear
    # first (this screen briefly shows one during its own navigation).
    await page.wait_for_timeout(3000)
    await _wait_for_blocking_layer_clear(page)
    try:
        await page.get_by_text("Loading...", exact=True).first.wait_for(state="hidden", timeout=60000)
    except Exception:
        pass
    await page.wait_for_timeout(2000)

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

    await step("entering_quantities")
    unfilled = await _fill_line_actual_quantities(page, item_products, item_qtys)
    if unfilled:
        await _click_button(page, "Close")
        return {"po_number": po_number, "status": "failed", "error": f"Could not enter Actual Quantity for item(s) {', '.join(unfilled)}"}

    await step("saving")
    if await _click_button(page, "Save and Close") != "clicked":
        await _click_button(page, "Close")
        return {"po_number": po_number, "status": "failed", "error": "Save and Close button not found"}
    await page.wait_for_timeout(6000)

    error_text = await _extract_error_text(page)
    if error_text:
        await _click_button(page, "Close")
        return {"po_number": po_number, "status": "failed", "error": _humanize_error(error_text)}
    # Sep 2 2026 (user's ask: "show SAP inbound number for user's
    # reference") - the same message strip that would carry an error
    # carries this success confirmation instead; not every tenant
    # response includes the ID in a parseable form, so this is
    # best-effort (None if it can't be found, never blocks the result).
    confirmation_text = await _extract_confirmation_text(page)
    return {"po_number": po_number, "status": "posted", "inbound_delivery_id": _extract_inbound_delivery_id(confirmation_text)}


STEPS_PER_PO = 4


async def post_goods_receipt_via_ui(po_items: dict, progress_cb=None) -> dict:
    """po_items: {po_number: {"supplier_doc_num": str, "bill_date": str,
    "item_qtys": {item_number: qty}, "item_products": {item_number:
    product_id}}} - one call per distinct PO, grouping every line item
    of that PO into a single submission (user's explicit ask, Aug 29
    2026), matching the manual flow exactly. `item_products` is required
    for the Actual Quantity grid to match the right row by Product ID
    (see _fill_line_actual_quantities' docstring for why row-order
    matching alone is not safe to trust).

    progress_cb(phase, current, total) - Sep 2 2026 (user's ask: real
    step-by-step visibility instead of a single spinner): `phase` is
    now one of "queued"/"logging_in"/"moving_stock"/"done", or
    "{step}:{po_number}:{idx}/{total_pos}" where step is one of
    "searching"/"opening_receipt"/"entering_quantities"/"saving" -
    wording never reveals SAP/Playwright to the end user, the caller
    (server.py) maps these to a plain-English progress bar. `current`/
    `total` count discrete steps across the WHOLE job (this function's
    steps + the Goods Movement step server.py reports after this
    returns), so a single progress bar/percentage stays consistent
    end to end - see `total_progress_steps()`.

    Returns {"results": [{"po_number", "status": "posted"|"failed"|
    "skipped", "error"?, "inbound_delivery_id"?}], "total_steps"}."""
    from playwright.async_api import async_playwright
    import playwright_concurrency

    po_numbers = list(po_items.keys())
    total_pos = len(po_numbers)
    total_steps = total_progress_steps(total_pos)
    results = []
    counter = {"n": 0}

    def _progress(phase: str) -> None:
        counter["n"] += 1
        if progress_cb:
            try:
                progress_cb(phase, counter["n"], total_steps)
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
                _progress("logging_in")
                await _login(page, username, password)
                for idx, po_number in enumerate(po_numbers, start=1):
                    spec = po_items[po_number]

                    def on_step(name, _po=po_number, _idx=idx):
                        _progress(f"{name}:{_po}:{_idx}/{total_pos}")

                    try:
                        result = await _post_one_po(
                            page, po_number, spec.get("supplier_doc_num"), spec.get("bill_date"),
                            spec.get("item_qtys") or {}, spec.get("item_products") or {}, on_step=on_step,
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
                    if result["status"] == "failed" and idx < total_pos:
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

    return {"results": results, "completed_at": datetime.now(timezone.utc).isoformat(), "total_steps": total_steps}


def total_progress_steps(total_pos: int) -> int:
    """1 (queued) + 1 (logging_in) + total_pos*STEPS_PER_PO (search/open/
    enter/save per PO) + 1 (moving_stock, reported by server.py after
    this module's function returns) - shared formula so server.py's
    progress bar percentage covers the WHOLE approve() job (Playwright
    GR + the Goods Movement SOAP call after it) as one consistent
    sequence, not two separate counters."""
    return 2 + total_pos * STEPS_PER_PO + 1
