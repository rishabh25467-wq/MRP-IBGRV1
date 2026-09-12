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
import asyncio
import logging
import re
from datetime import datetime, timezone

from sap_playwright_pgr_service import _login, _wait_for_blocking_layer_clear, _extract_error_text, _humanize_error, _click_button
import object_storage_client

logger = logging.getLogger(__name__)


async def _capture_failure(page, po_number: str, step: str, error: str) -> dict:
    """Sep 11 2026, user's explicit ask ("any tool we can build to give
    u that insight") - every failure path now uploads a real screenshot
    of the exact SAP screen state to Object Storage (not local disk,
    which is invisible/unshareable once deployed - production runs in a
    separate container than this preview) so staff can open it directly
    from the GRN Approval screen instead of a bare error string.

    Sep 11 2026 follow-up (user's explicit ask, "make sure u do not slow
    things down"): the actual upload is a blocking `requests` call -
    running it directly here would stall the asyncio event loop (this
    Playwright job's own page, plus every other concurrent GRN job/API
    request sharing the same loop) for as long as the upload takes.
    Offloaded to a worker thread via asyncio.to_thread so it can never
    add latency to anything else."""
    screenshot_path = None
    try:
        png = await page.screenshot()
        screenshot_path = await asyncio.to_thread(object_storage_client.upload_failure_screenshot, png, po_number)
    except Exception as e:
        logger.warning(f"Could not capture failure screenshot for PO {po_number}: {e}")
    return {"po_number": po_number, "status": "failed", "error": error, "failed_step": step, "screenshot_path": screenshot_path}


async def _ensure_draft_discarded(page, po_number: str) -> None:
    """Sep 10 2026, real incident: SAP confirmed Inbound Delivery Request
    60063 (PO 29118) stuck LOCKED BY ITADMIN (our own automation user) -
    every prior error-exit here only ever fired-and-forgot a single
    `_click_button(page, "Close")` with no check it actually worked, so
    on any tenant response where "Close" isn't the visible/clickable
    label at that moment (blocked by the very error banner it's meant to
    dismiss, a slightly different Fiori label, etc.) the "Create Inbound
    Delivery and Goods Receipt" draft this PO's row opened was silently
    abandoned still checked out - every later Retry then just re-opens
    (and re-locks under the same user) that same stuck draft, hence the
    IDENTICAL SAP error on every single retry. Now tries "Close", then
    "Cancel" as an alternate label some Fiori error states use, then
    falls back to Escape - never raises (best-effort cleanup only, must
    never block returning the real result to the caller)."""
    try:
        if await _click_button(page, "Close") == "clicked":
            return
        if await _click_button(page, "Cancel") == "clicked":
            return
        await page.keyboard.press("Escape")
    except Exception as e:
        logger.warning(f"Could not confirm the SAP draft for PO {po_number} was discarded (may still be locked): {e}")


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

    def _find_po_id_label():
        return page.query_selector_all("label")

    async def _po_id_input():
        for lbl in await _find_po_id_label():
            if (await lbl.inner_text()).strip() == "Purchase Order ID" and await lbl.is_visible():
                for_id = await lbl.get_attribute("for")
                if for_id:
                    return await page.query_selector(f"#{for_id}")
        return None

    # Sep 2 2026 fix (found while batch-reading multiple POs in one
    # session for fetch_open_po_quantities): this toggle button CLOSES
    # the panel if it's already open from a PREVIOUS PO's search in the
    # same session (navigating into/out of a PO detail screen does NOT
    # reset it) - clicking blindly every time is a coin flip. Checks
    # for the real input first; only toggles (and re-checks) if it's
    # not there yet, so this stays correct whichever state it starts in.
    po_id_input = await _po_id_input()
    if po_id_input is None:
        await filter_btn.click(force=True)
        await page.wait_for_timeout(1500)
        po_id_input = await _po_id_input()
    if po_id_input is None:
        raise RuntimeError("Could not find the 'Purchase Order ID' filter field")
    await po_id_input.fill(po_number)
    go_btn = page.get_by_text("Go", exact=True).first
    if await go_btn.count() > 0:
        await go_btn.click(force=True)
        # Sep 12 2026 fix (real incident, PO 29455 crashed at
        # "unexpected_crash" - the failure screenshot showed a
        # COMPLETELY DIFFERENT, unrelated PO still highlighted in the
        # list): a fixed 4000ms wait was not always long enough for this
        # tenant to actually re-render the filtered table after "Go" -
        # the old blind wait then read the row count (and later, row[0])
        # from the STALE pre-filter table, so the automation went on to
        # click/act on the wrong PO's row entirely. Same
        # loading-text-based wait already proven reliable elsewhere in
        # this module (_wait_for_table_load) now gates this too.
        await page.wait_for_timeout(1500)
        await _wait_for_table_load(page)
    return len(await page.query_selector_all('tr[id^="__table"]'))


async def _fill_line_actual_quantities(page, item_products: dict, item_qtys: dict, po_number: str = None) -> list:
    """item_products: {item_number: product_id}, item_qtys: {item_number:
    qty}. Matches each grid row to a PO line, never by row order (a real,
    confirmed-dangerous bug found by testing_agent code review, Aug 29
    2026: order-based matching could silently post a quantity against
    the WRONG line on a real PO if the dialog's own row order ever
    differs from the shipment's item order).

    Sep 12 2026 BUG FOUND + FIXED (real incident, PO 29455 - a genuine
    5-line PO where EVERY line is the SAME Product ID, split across
    delivery items 10/20/30/40/50): Product ID alone is NOT a unique
    match key once a PO has more than one line for the identical
    product - matching by Product ID always resolved to the very FIRST
    such row for every item_number, so items 20/30/40/50 never got
    their own Actual Quantity filled at all (confirmed live: those
    cells stayed empty, SAP's own "Actual quantity ... missing"
    validation fired on save), while item 10's single cell was silently
    overwritten by each later item's quantity in turn. SAP's own
    "Reference Details Description" grid column echoes back our own
    "{po_number}-{item_number}" reference for each line (confirmed live
    in the crash screenshot - "29455-1".."29455-5") - a genuinely unique
    per-row key, tried FIRST when `po_number` is given; Product ID stays
    as the fallback for the single-line-per-product case or if that
    reference text isn't present. `used_rows` stops two different
    item_numbers from ever matching the SAME row via the Product ID
    fallback (the exact class of bug this whole fix addresses).

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
    row_texts = [await row.inner_text() for row in rows]
    used_rows = set()
    unfilled = []
    for item_number, qty in item_qtys.items():
        product_id = item_products.get(item_number)
        reference_key = f"{po_number}-{item_number}" if po_number else None
        matched_idx = None
        if reference_key:
            for i, text in enumerate(row_texts):
                if i not in used_rows and reference_key in text:
                    matched_idx = i
                    break
        if matched_idx is None and product_id:
            for i, text in enumerate(row_texts):
                if i not in used_rows and product_id in text:
                    matched_idx = i
                    break
        if matched_idx is None:
            unfilled.append(item_number)
            continue
        used_rows.add(matched_idx)
        matched_row = rows[matched_idx]
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
    # Sep 12 2026 fix (real incident, PO 29455): `hits` being non-zero
    # only ever meant "some row exists", never that it's really OUR PO -
    # a stale/still-loading table (see _search_po_exact's own fix above)
    # could return a leftover row from a PREVIOUS PO's search. Verify
    # the actual row text before ever clicking/acting on it; if it still
    # doesn't match after one clean re-search, fail with a precise,
    # diagnosable reason instead of crashing generically deep inside the
    # Post Goods Receipt screen for the WRONG PO.
    row_text = await rows[0].inner_text()
    if po_number not in row_text:
        await _wait_for_table_load(page)
        rows = await page.query_selector_all('tr[id^="__table"]')
        row_text = (await rows[0].inner_text()) if rows else ""
        if not rows or po_number not in row_text:
            return await _capture_failure(
                page, po_number, "searching",
                f"SAP's Purchase Orders list did not refresh to show PO {po_number} after searching - it was still showing a different PO's row",
            )
    await _click_po_row(page, rows[0], po_number)
    await page.wait_for_timeout(1500)

    click_result = await _click_button(page, "Post Goods Receipt")
    if click_result == "disabled":
        return await _capture_failure(page, po_number, "opening_receipt", "Post Goods Receipt is disabled for this PO in SAP")
    if click_result == "not_found":
        return await _capture_failure(page, po_number, "opening_receipt", "Post Goods Receipt button not found for this PO")
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
    unfilled = await _fill_line_actual_quantities(page, item_products, item_qtys, po_number)
    if unfilled:
        result = await _capture_failure(page, po_number, "entering_quantities", f"Could not enter Actual Quantity for item(s) {', '.join(unfilled)}")
        await _ensure_draft_discarded(page, po_number)
        return result

    # Sep 12 2026 fix (real incident, PO 29455 - user's explicit
    # clarification): "Post Goods Receipt" on a PO opens SAP's Inbound
    # Delivery draft with ALL of that PO's still-open lines by default,
    # not just the ones this particular shipment actually covers. A PO
    # with 5 open lines but a delivery for only 1 of them left the
    # other 4 sitting with a blank/zero Actual Quantity - SAP's own
    # save-time validation then rejected the whole draft ("Actual
    # quantity for Delivery Item ID 20 missing"...) rather than simply
    # ignoring the lines nobody delivered. `item_qtys` only ever
    # contains what THIS shipment covers, so any other line is
    # correctly left untouched (still zero) by the fill step above -
    # SAP's own toolbar action removes exactly those before Save,
    # matching the real intended behavior: only the lines that actually
    # arrived get received, everything else is simply not part of this
    # delivery at all.
    await _click_button(page, "Remove Zero Quantity Items")
    await page.wait_for_timeout(1000)

    await step("saving")
    if await _click_button(page, "Save and Close") != "clicked":
        result = await _capture_failure(page, po_number, "saving", "Save and Close button not found")
        await _ensure_draft_discarded(page, po_number)
        return result
    await page.wait_for_timeout(6000)

    error_text = await _extract_error_text(page)
    if error_text:
        result = await _capture_failure(page, po_number, "saving", _humanize_error(error_text))
        await _ensure_draft_discarded(page, po_number)
        return result
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
                        result = await _capture_failure(page, po_number, "unexpected_crash", "Could not reach SAP's receipt screen - please retry")
                    results.append(result)
                    if result["status"] == "failed" and idx < total_pos:
                        try:
                            await page.close()
                        except Exception:
                            pass
                        page = await browser.new_page(viewport={"width": 1600, "height": 900})
                        await _login(page, username, password)
            finally:
                # Sep 2 2026 (user's explicit ask, auto-retry safety):
                # browser.close() is a real I/O call to the Chromium
                # subprocess and could theoretically raise (e.g. if it
                # already crashed) - if it did, that exception used to
                # propagate past the `return` below and DISCARD every
                # already-posted PO's result, which would make a caller-
                # level retry unsafe (it would re-attempt POs that
                # actually succeeded). Swallowing it here guarantees this
                # function always returns whatever `results` it has.
                try:
                    await browser.close()
                except Exception:
                    pass
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


# Sep 2 2026: the Playwright-based `fetch_open_po_quantities` that used
# to live here was CANCELLED per user's explicit ask ("we cannot go with
# playwright for this") - it was live-hammering SAP's UI sequentially
# for every active PO (10-20s each) with a ~100% failure rate (SAPUI5's
# view-selector auto-closing on a re-click of an already-selected
# value), and was found starving other concurrent SAP calls. Replaced
# with `sap_po_analytics_client.py`'s single fast OData analytics query
# - see that module's docstring.
