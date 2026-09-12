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

Sep 12 2026 - HYBRID ARCHITECTURE, replaces the PO-search half of the
flow above. The user's SAP Admin set up a new Communication Arrangement
("Input of Advanced Shipping Notification") exposing
`ManageStandardInboundDeliveryNotificationIn` (see
sap_inbound_delivery_notification_client.py). `_post_one_po` now:
  1. Calls that SOAP service directly (`MaintainBundle`, actionCode=01,
     release=False) - creates the Inbound Delivery Notification
     referencing this PO's items by PO+ItemID (never by product text),
     with the real Delivery Notification ID/Date/Vendor/Quantity
     already set. Confirmed live (Sep 12 2026, real PO 29482, all 5
     lines sharing one product) this structurally eliminates the whole
     class of PO-search-flakiness and multi-line-same-product
     row-matching bugs the OLD flow (`_search_po_exact`/`_click_po_row`,
     removed) kept hitting - there is no PO search or row-matching left
     to get wrong, SAP resolves the PO+ItemID reference itself.
  2. Playwright then opens that notification BY ITS OWN ID (not by PO
     number) in the Inbound Delivery Notifications view - reusing
     `_open_inbound_delivery_notifications`/`_search_delivery` from
     sap_playwright_pgr_service.py (same underlying SAP business object
     as the STO flow, confirmed live). Only Actual Quantity entry +
     Save and Close remain manual/UI-driven - releasing the notification
     via SOAP was tried and confirmed to require a task-based Warehouse
     Logistics Model this tenant doesn't use for inbound (real live
     test, Sep 12 2026: release succeeded but Delivery Status stayed
     "Not Started", and neither InboundDeliveryRelease nor
     InboundDeliveryPGRBackground OData actions can complete it either -
     both return "action is disabled", the same KBA 3583076 restriction
     documented in inbound_receipt_service.py). Save and Close (PGR
     Ground, non-task-based) is the only path that actually completes
     the Goods Receipt on this tenant.

Scope (user's explicit ask, Aug 29 2026): Goods Receipt only. Invoice
creation (a separate SAP screen, Supplier Invoicing work center) is a
later phase, done by a different user - never touched here.

UNVERIFIED end-to-end (no sandbox/dry-run exists for this - every SAP
write here is real and irreversible). The SOAP create step and the
Playwright open-by-ID + Save and Close were each confirmed live on a
real test PO (Sep 12 2026) - the very first real call into
post_goods_receipt_via_ui on a genuine multi-PO/multi-line shipment
MUST still be supervised/reviewed by the user before this is trusted
for unattended use."""
import asyncio
import logging
import re
from datetime import datetime, timezone

from sap_playwright_pgr_service import (
    _login, _wait_for_blocking_layer_clear, _extract_error_text, _humanize_error, _click_button,
    _open_inbound_delivery_notifications, _search_delivery,
)
import object_storage_client

logger = logging.getLogger(__name__)


async def _capture_failure(page, po_number: str, step: str, error: str, events: list = None) -> dict:
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
    add latency to anything else.

    Sep 12 2026, user's explicit ask ("a flow of events that happened
    in playwright successfully before it hit a failure") - `events` is
    whatever milestone log `_post_one_po` had already built up before
    this failure, always included even on a crash caught by the OUTER
    try/except (that caller passes its own possibly-partial list in)."""
    screenshot_path = None
    try:
        png = await page.screenshot()
        screenshot_path = await asyncio.to_thread(object_storage_client.upload_failure_screenshot, png, po_number)
    except Exception as e:
        logger.warning(f"Could not capture failure screenshot for PO {po_number}: {e}")
    return {"po_number": po_number, "status": "failed", "error": error, "failed_step": step, "screenshot_path": screenshot_path, "events": events or []}


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


async def _switch_to_all_deliveries_view(page) -> bool:
    """Inbound Delivery Notifications defaults to "Advised Delivery
    Notifications" (STO-originated only) - our SOAP-created supplier
    notifications don't appear there until the base view is switched to
    "All Delivery Notifications by Selection" (confirmed live, Sep 12
    2026: 0 hits in the default view, 1 hit immediately after this
    switch, for the identical ID).

    Sep 12 2026 BUG FOUND + FIXED, TWICE, real incidents:
    (1) the old selector `.sapMSelect, [role="combobox"]").first` is
    not scoped to this page's own view-selector dropdown at all - it
    can just as easily match the SAP Fiori shell's own "All
    Categories" global-search dropdown at the very top of the screen
    (loads first, same CSS class), silently opening/clicking THAT
    instead and leaving "Advised Delivery Notifications" untouched.
    (2) switching to matching by visible text instead ("Advised
    Delivery Notifications") fixed that, but crashed on a real
    multi-PO shipment's SECOND po: `get_by_text(...).first` matched a
    STALE, HIDDEN `<li role="option">` left in the DOM from the FIRST
    PO's own earlier interaction with this exact dropdown (same page/
    browser session is reused across every PO in one shipment) instead
    of the actual visible closed-state trigger - `force=True` can't
    click an element with no bounding box, so `.click()` raised
    "Element is not visible" and crashed the whole PO
    (unexpected_crash), confirmed via the app's own error logs. Real
    fix: explicitly filter candidates to the one that `is_visible()`,
    never trust `.first` alone when the same text can legitimately
    exist twice (once as the live trigger, once as a leftover/hidden
    option from a previous open+close).

    Returns False (never raises) if no visible match is found, so the
    caller can retry the whole thing instead of crashing the PO."""
    candidates = page.get_by_text("Advised Delivery Notifications", exact=True)
    trigger = None
    for i in range(await candidates.count()):
        el = candidates.nth(i)
        if await el.is_visible():
            trigger = el
            break
    if trigger is None:
        return False
    await trigger.click(force=True)
    await page.wait_for_timeout(1000)
    for o in await page.query_selector_all('li, [role="option"]'):
        if (await o.inner_text()).strip() == "All Delivery Notifications by Selection" and await o.is_visible():
            await o.click(force=True)
            await page.wait_for_timeout(2500)
            return True
    return False


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


async def _click_po_row(page, row, row_label: str) -> None:
    """SELECTS (does not navigate into) the row so the list's own
    toolbar "Post Goods Receipt" button becomes enabled - matches the
    real manual flow ("search the exact PO -> select its row -> 'Post
    Goods Receipt' button", see module docstring). Reused unchanged for
    the Sep 12 2026 hybrid flow's Inbound Delivery Notifications list -
    same row-selection-indicator-column trick applies there too. Sep 2
    2026 BUG FOUND + FIXED (same investigation as the PO-search fix
    above), THREE attempts: (1) original `row.click()` landed at the
    row's bounding-box CENTER, which falls on the "Supplier Name" cell
    and navigates to that Business Partner's own detail screen instead
    of selecting the row; (2) clicking the first `<td>`'s link was
    wrong too - that cell has no link, it's an empty row-selection
    indicator column; (3) clicking the "Purchase Order ID" link cell
    (matched by its own text) navigates INTO the PO's own detail screen
    - confirmed live that screen has NO "Post Goods Receipt" button at
    all (it's a LIST-toolbar-only action, requires the row merely
    selected, not opened). Real fix: click the empty first `<td>` (the
    selection indicator column itself, no link) - this selects the row
    in place without navigating anywhere."""
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


async def _post_one_po(page, po_number: str, doc_code: str, supplier_doc_num: str, bill_date: str, item_qtys: dict, item_products: dict,
                        item_uoms: dict, vendor_code: str, notification_client, on_step=None, events: list = None) -> dict:
    # Sep 12 2026, user's explicit ask - a plain-English trail of every
    # milestone actually reached in SAP before a failure (or success),
    # surfaced verbatim in the GRN Approval screen's new "Diagnostics"
    # modal. Mutated in place (not just returned) so the OUTER
    # try/except in post_goods_receipt_via_ui still has whatever was
    # logged so far even when this whole function raises unhandled.
    if events is None:
        events = []

    async def step(name):
        if on_step:
            on_step(name)

    await step("searching")
    # Sep 12 2026 - the whole PO-search step is now a single SOAP call
    # (see module docstring): creates the Inbound Delivery Notification
    # referencing each item by PO+ItemID directly, so there is no PO
    # search or row-matching left to get wrong.
    #
    # Sep 12 2026 BUG FOUND + FIXED (real incident: two DIFFERENT real
    # shipments, 5XL5EE (supplier_doc_num "test_grn") and FYXUKN
    # (supplier_doc_num "test_GRN"), both for PO 29482 - SAP normalizes/
    # uppercases Delivery Notification IDs, so the OLD scheme
    # (`{supplier_doc_num}-{po_number}`) silently collided: FYXUKN's
    # SOAP create hit "already exists" (correctly handled below) and
    # then went on to open/select/report success on 5XL5EE's ALREADY-
    # FINISHED document instead of its own - FYXUKN's actual goods were
    # never received in SAP at all. `notification_id` now always keys
    # off `doc_code` (this app's own unique 6-char shipment code, never
    # user-typed, never case-collidable in practice) instead of the
    # free-text supplier_doc_num - guaranteed unique per shipment+PO.
    delivery_date = (bill_date or datetime.now(timezone.utc).strftime("%Y-%m-%d"))[:10]
    notification_id = f"{doc_code}-{po_number}"
    # Sep 12 2026 fix (real incident, PO 29456: SAP rejected the SOAP
    # create with the confusing "No inbound delivery request exists
    # for purchase order reference 29456 - 1" - traced to this PO
    # line's cached product_id genuinely being None in SAP's own PO
    # data, a pre-existing data-quality gap, not something this flow
    # can resolve). Catch it here with a clear, actionable message
    # instead of sending a literal "None" string as the SOAP
    # ItemProduct/ProductID and letting SAP's own confusing error
    # surface instead.
    missing_products = [item_number for item_number in item_qtys if not item_products.get(item_number)]
    if missing_products:
        return {
            "po_number": po_number, "status": "skipped",
            "error": f"Cannot receive PO {po_number} item(s) {', '.join(missing_products)} via SAP - Product ID is missing in the cached PO data (ask SAP Admin to check this PO line's product master link)",
            "events": events,
        }
    soap_items = [
        {"item_number": item_number, "quantity": qty, "unit_of_measure": item_uoms.get(item_number) or "EA", "product_id": item_products.get(item_number)}
        for item_number, qty in item_qtys.items()
    ]
    try:
        await asyncio.to_thread(notification_client.maintain_bundle, notification_id, po_number, vendor_code, delivery_date, soap_items, False)
        events.append(f"Created Inbound Delivery Notification {notification_id} in SAP (SOAP, references PO {po_number} directly)")
    except Exception as e:
        # Sep 12 2026 - a Retry re-derives the SAME notification_id
        # (it's built from supplier_doc_num+po_number, both fixed on
        # the shipment doc) and re-calls this on a doc that already
        # exists in SAP from the PREVIOUS attempt (e.g. the earlier
        # attempt got this far but failed further down, in the UI
        # steps) - SAP correctly rejects the duplicate create. Treat
        # that specific case as already-done and continue to search
        # for it, instead of failing the whole retry outright.
        if "already exist" in str(e).lower():
            events.append(f"Inbound Delivery Notification {notification_id} already exists in SAP from a previous attempt - continuing")
        else:
            return {"po_number": po_number, "status": "skipped", "error": f"Could not create the Inbound Delivery Notification in SAP for PO {po_number}: {e}", "events": events}

    await _open_inbound_delivery_notifications(page)
    events.append("Opened Inbound Logistics - Inbound Delivery Notifications")
    view_switched = await _switch_to_all_deliveries_view(page)
    events.append("Switched to 'All Delivery Notifications by Selection' view" if view_switched else "Could not switch view - will retry")
    # Sep 12 2026 fix (real incident, shipment 5XL5EE/PO 29482): SAP's
    # UI search index lags a few seconds behind a SOAP create - the
    # notification exists (confirmed: SOAP call above already
    # succeeded) but doesn't show up in the list on the very first
    # search. Retry the search itself (not just re-navigate) with a
    # short backoff before giving up. Re-attempt the view switch each
    # time too (see that function's docstring for the real incident
    # where it silently clicked the wrong dropdown and never retried).
    hits = 0
    for attempt in range(4):
        if not view_switched:
            view_switched = await _switch_to_all_deliveries_view(page)
        hits = await _search_delivery(page, notification_id)
        if hits > 0:
            break
        await page.wait_for_timeout(4000)
    if hits == 0:
        return await _capture_failure(
            page, po_number, "searching",
            f"Notification {notification_id} was created in SAP but could not be found in the list after retrying - may still be indexing, please Retry",
            events=events,
        )
    rows = await page.query_selector_all('tr[id^="__table"]')
    events.append(f"Found Notification {notification_id} in the list")
    await _click_po_row(page, rows[0], notification_id)
    await page.wait_for_timeout(1500)
    events.append(f"Selected Notification {notification_id}'s row")

    click_result = await _click_button(page, "Post Goods Receipt")
    if click_result == "disabled":
        return await _capture_failure(page, po_number, "opening_receipt", "Post Goods Receipt is disabled for this notification in SAP", events=events)
    if click_result == "not_found":
        return await _capture_failure(page, po_number, "opening_receipt", "Post Goods Receipt button not found for this notification", events=events)
    events.append("Clicked 'Post Goods Receipt'")
    await step("opening_receipt")
    # Sep 2 2026 fix (same investigation as the search/row-select fixes
    # above): the "Create Inbound Delivery and Goods Receipt" screen's
    # own Line Items grid shows an inline "Loading..." text while its
    # data fetches - a fixed 4000ms wait was shorter than this tenant's
    # real load time, so `_fill_line_actual_quantities` used to run
    # against an empty/still-loading grid and report every item "could
    # not be filled". Also waits for the full-page blocking overlay to
    # clear first (this screen briefly shows one during its own
    # navigation).
    await page.wait_for_timeout(3000)
    await _wait_for_blocking_layer_clear(page)
    try:
        await page.get_by_text("Loading...", exact=True).first.wait_for(state="hidden", timeout=60000)
    except Exception:
        pass
    await page.wait_for_timeout(2000)
    events.append("Opened Create Inbound Delivery and Goods Receipt screen")
    # Delivery Notification ID / Actual Delivery Date / Vendor are
    # already pre-filled from the SOAP create above (confirmed live,
    # Sep 12 2026) - no more manual label-lookup + fill needed here.

    await step("entering_quantities")
    # Sep 12 2026: the dialog now only ever shows the exact lines THIS
    # notification was created with (one row per item_qtys entry, via
    # the SOAP call above) - unlike the old PO-search flow, there are no
    # other-lines-of-the-same-PO to worry about, so "Remove Zero
    # Quantity Items" is no longer needed at all.
    unfilled = await _fill_line_actual_quantities(page, item_products, item_qtys, po_number)
    if unfilled:
        result = await _capture_failure(page, po_number, "entering_quantities", f"Could not enter Actual Quantity for item(s) {', '.join(unfilled)}", events=events)
        await _ensure_draft_discarded(page, po_number)
        return result
    events.append(f"Entered Actual Quantity for item(s): {', '.join(item_qtys.keys())}")

    await step("saving")
    if await _click_button(page, "Save and Close") != "clicked":
        result = await _capture_failure(page, po_number, "saving", "Save and Close button not found", events=events)
        await _ensure_draft_discarded(page, po_number)
        return result
    events.append("Clicked 'Save and Close'")
    await page.wait_for_timeout(6000)

    error_text = await _extract_error_text(page)
    if error_text:
        result = await _capture_failure(page, po_number, "saving", _humanize_error(error_text), events=events)
        await _ensure_draft_discarded(page, po_number)
        return result
    # Sep 2 2026 (user's ask: "show SAP inbound number for user's
    # reference") - the same message strip that would carry an error
    # carries this success confirmation instead; not every tenant
    # response includes the ID in a parseable form, so this is
    # best-effort (None if it can't be found, never blocks the result).
    confirmation_text = await _extract_confirmation_text(page)
    events.append("SAP confirmed the Goods Receipt was posted")
    return {"po_number": po_number, "status": "posted", "inbound_delivery_id": _extract_inbound_delivery_id(confirmation_text), "events": events}


STEPS_PER_PO = 4


async def post_goods_receipt_via_ui(po_items: dict, notification_client, progress_cb=None) -> dict:
    """po_items: {po_number: {"doc_code": str, "supplier_doc_num": str,
    "bill_date": str, "vendor_code": str, "item_qtys": {item_number:
    qty}, "item_products": {item_number: product_id}, "item_uoms":
    {item_number: unit_code}}} - one call per distinct PO, grouping every line item of that PO into
    a single submission (user's explicit ask, Aug 29 2026), matching
    the manual flow exactly. `notification_client` is a
    SAPInboundDeliveryNotificationClient (see that module) - the SOAP
    create step run inside `_post_one_po` before any browser
    navigation. `item_products`/`item_uoms` feed both the SOAP create
    call and the Actual Quantity grid match by Product ID (see
    _fill_line_actual_quantities' docstring for why row-order matching
    alone is not safe to trust).

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

                    events = [f"Logged in to SAP as {username}"]
                    try:
                        result = await _post_one_po(
                            page, po_number, spec.get("doc_code"), spec.get("supplier_doc_num"), spec.get("bill_date"),
                            spec.get("item_qtys") or {}, spec.get("item_products") or {}, spec.get("item_uoms") or {},
                            spec.get("vendor_code"), notification_client, on_step=on_step, events=events,
                        )
                    except Exception as e:
                        logger.error(f"Playwright Supplier GRN failed for PO {po_number}: {e}")
                        result = await _capture_failure(page, po_number, "unexpected_crash", "Could not reach SAP's receipt screen - please retry", events=events)
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

    return {"results": results, "completed_at": datetime.now(timezone.utc).isoformat(), "total_steps": total_steps, "sap_username": username}


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
