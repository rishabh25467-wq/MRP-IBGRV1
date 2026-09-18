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
    caller can retry the whole thing instead of crashing the PO.

    Sep 13 2026 update: the SAP Admin changed this screen's own
    personalization so it now DEFAULTS to "All Delivery Notifications by
    Selection" directly (no more "Advised Delivery Notifications" to
    switch away from) - without this check, every run would keep
    "failing" to find that old default label and burn through the
    caller's full retry/backoff loop (~16s) for nothing, while also
    logging a confusing "Could not switch view" event on an already-
    correct screen. Checked first, before touching the dropdown at all;
    falls back to the old click-based switch if the personalization
    ever reverts or differs for a different SAP login."""
    already_all = page.get_by_text("All Delivery Notifications by Selection", exact=True)
    for i in range(await already_all.count()):
        if await already_all.nth(i).is_visible():
            return True
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


async def _is_notification_finished_in_details_panel(page) -> bool:
    """Sep 14 2026 CORRECTED fix (continuation of the Sep 13 2026
    "already Finished" detection - the first attempt at this, which
    tried clicking an ID link to "navigate into" a detail screen, was
    WRONG - disproven by the user's own live screenshot, real PO 29482/
    notification TEST_0506-WFRRSL-29482): the list's own columns
    (Delivery Notification ID/Status/Release Status/Planned Delivery/
    Sender Name/Created By/Delivery Type) never include "Delivery
    Status" at all - that field only ever appears in the "Details:
    Delivery Notification ..." panel SAP's own master-detail layout
    renders BELOW the list the INSTANT a row is selected (confirmed
    live: `_click_po_row` above already selects the row before this
    check even runs, and the screenshot shows that Details panel
    already fully populated with "Delivery Status: Finished" - no
    extra click/navigation was ever needed, that's what made the first
    attempt's ID-link click a no-op - there IS no such link in the
    list row). Scans the whole page's text line-by-line for a line
    containing "Delivery Status" and checks that SAME line (or, for a
    stacked label/value layout, the next non-empty line) for
    "Finished" - deliberately NOT a blind "finish" anywhere on the
    page, since a status filter dropdown's own hidden option list can
    genuinely contain the word "Finished" in its DOM regardless of
    the current document's real status."""
    text = await page.inner_text("body")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for i, line in enumerate(lines):
        if "delivery status" not in line.lower():
            continue
        if "finish" in line.lower():
            return True
        if i + 1 < len(lines) and "finish" in lines[i + 1].lower():
            return True
    return False


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


MAX_NOTIFICATION_ID_LENGTH = 35
MAX_INVOICE_PREFIX_LENGTH = 20


def _build_notification_id(supplier_doc_num: str, doc_code: str, po_number: str) -> str:
    """Sep 13 2026, user's explicit ask ("can we go for a number like
    inv number / doc code") - the supplier's own invoice/bill number is
    now visible in the SAP Delivery Notification ID for traceability,
    while keeping the exact uniqueness guarantee from the Sep 12 fix
    above (`{doc_code}-{po_number}` alone, never user-typed text, never
    case-collidable) as a fixed SUFFIX that's always present - so two
    shipments whose supplier-typed invoice numbers collide (even after
    SAP's own case-normalization) still can never produce the same ID.
    Aug 2026 update (user confirmed "SAP accepts /") - the invoice
    number is no longer character-sanitized (SAP's SOAP field accepts
    "/" and other characters fine, and it's XML-escaped before going
    on the wire in sap_inbound_delivery_notification_client.py), and is
    capped at MAX_INVOICE_PREFIX_LENGTH (20) chars instead of eating
    whatever space happens to be left after the suffix, while the
    overall ID still respects SAP's ~35-char BusinessTransactionDocum-
    entID limit. Falls back to the old suffix-only ID when there's no
    invoice number on file (field is optional)."""
    suffix = f"{doc_code}-{po_number}"
    prefix = (supplier_doc_num or "").strip()
    if not prefix:
        return suffix
    max_prefix_len = min(MAX_INVOICE_PREFIX_LENGTH, MAX_NOTIFICATION_ID_LENGTH - len(suffix) - 1)
    if max_prefix_len <= 0:
        return suffix
    return f"{prefix[:max_prefix_len]}-{suffix}"


async def _post_one_po(page, po_number: str, doc_code: str, supplier_doc_num: str, bill_date: str, item_qtys: dict, item_products: dict,
                        item_uoms: dict, vendor_code: str, notification_client, confirmation_report_client, on_step=None, events: list = None) -> dict:
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
    # (`{supplier_doc_num}-{po_number}`) silently collided: the SECOND
    # create went on to open/select/report success on the FIRST
    # shipment's ALREADY-FINISHED document instead of its own - the
    # second shipment's actual goods were never received in SAP at all.
    # `notification_id` now always keys off `doc_code` (this app's own
    # unique 6-char shipment code, never user-typed, never
    # case-collidable in practice) instead of the free-text
    # supplier_doc_num - guaranteed unique per shipment+PO.
    #
    # Aug 2026 correction (confirmed live, user's own SAP screenshot):
    # SAP does NOT reject a duplicate DeliveryNotificationID at create
    # time - it happily creates a brand new, separate document with the
    # identical ID (Delivery ID is SAP's own real unique key; Delivery
    # Notification ID is a plain user-entered reference field with no
    # uniqueness constraint at all). Confirmed via a real incident this
    # same session: a single shipment (DSWRPG, PO 29482) accumulated 24
    # separate duplicate Inbound Delivery Notification documents, and
    # SAP's own analytics report confirmed AT LEAST 2 of them (53015,
    # 53018) each independently reached "Finished" - i.e. genuinely
    # double-posted Goods Receipt, not just harmless duplicate headers.
    # See the new confirmation-check right below - it is the ONLY thing
    # standing between a Retry and a real duplicate Goods Receipt, since
    # SAP itself provides no protection here.
    #
    # Sep 13 2026 update (user's explicit ask, "can we go for a number
    # like inv number / doc code") - the supplier's invoice number is
    # now prepended for traceability via `_build_notification_id`,
    # WITHOUT reopening the collision bug above: `{doc_code}-{po_number}`
    # is always kept intact as the tail, so uniqueness never depends on
    # the free-text invoice number even if two suppliers reuse the same
    # one.
    delivery_date = (bill_date or datetime.now(timezone.utc).strftime("%Y-%m-%d"))[:10]
    notification_id = _build_notification_id(supplier_doc_num, doc_code, po_number)
    # Aug 2026, real incident found this session (see above) - a Retry
    # re-derives this SAME notification_id and, since SAP never rejects
    # a duplicate create, would otherwise create yet another separate
    # document and risk posting Goods Receipt on it too (real double
    # receipt, confirmed live). Check SAP's own "Inbound Delivery
    # Detailed Details" analytics report FIRST - if it already shows a
    # real confirmed Goods Receipt for this exact notification_id+PO,
    # this PO is already done; skip the create AND every Playwright
    # step entirely rather than risk a second posting.
    try:
        already_confirmed = await asyncio.to_thread(confirmation_report_client.find_confirmation_rows, po_number, notification_id)
    except Exception as e:
        already_confirmed = []
        events.append(f"Could not pre-check SAP for an existing confirmation on PO {po_number} (continuing): {e}")
    if already_confirmed:
        # Sep 16 2026 fix - same "latest wins" resolution (see the other
        # occurrence below) - don't blindly trust the first row's ID.
        delivery_ids = [r.get("CDELIVERY_UUID") for r in already_confirmed if r.get("CDELIVERY_UUID")]
        try:
            existing_delivery_id = max(delivery_ids, key=int) if delivery_ids else None
        except (TypeError, ValueError):
            existing_delivery_id = sorted(delivery_ids)[-1] if delivery_ids else None
        events.append(f"PO {po_number} already has a confirmed Goods Receipt in SAP for notification {notification_id} (Inbound Delivery {existing_delivery_id}) - skipping re-creation to avoid a duplicate receipt")
        return {"po_number": po_number, "status": "posted", "inbound_delivery_id": existing_delivery_id, "events": events}
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
    skipped_items = []
    if missing_products:
        # Sep 13 2026, user's explicit ask ("if 1 PO or 1 line is skipped,
        # other POs should get written with GRN completed") - this used to
        # abort the WHOLE PO whenever ANY one of its lines had this
        # data-quality gap, even when the rest of the PO's lines were
        # perfectly fine. Now only the bad line(s) are dropped (kept out
        # of soap_items/item_qtys below, so neither the SOAP create nor
        # the Actual Quantity fill ever sees them) - the good lines still
        # go through. Only skip the ENTIRE PO if every single line on it
        # has this problem (nothing valid left to post).
        skipped_items = [{"item_number": item_number, "reason": "Product ID is missing in the cached PO data (ask SAP Admin to check this PO line's product master link)"} for item_number in missing_products]
        events.append(f"Skipping item(s) {', '.join(missing_products)} on PO {po_number} - Product ID missing in cached PO data; remaining valid line(s) will still be processed")
        item_qtys = {k: v for k, v in item_qtys.items() if k not in missing_products}
        if not item_qtys:
            return {
                "po_number": po_number, "status": "skipped",
                "error": f"Cannot receive PO {po_number} via SAP - Product ID is missing in the cached PO data for item(s) {', '.join(missing_products)} (ask SAP Admin to check this PO line's product master link)",
                "skipped_items": skipped_items, "events": events,
            }
    soap_items = [
        {"item_number": item_number, "quantity": qty, "unit_of_measure": item_uoms.get(item_number) or "EA", "product_id": item_products.get(item_number)}
        for item_number, qty in item_qtys.items()
    ]
    try:
        await asyncio.to_thread(notification_client.maintain_bundle, notification_id, po_number, vendor_code, delivery_date, soap_items, False)
        events.append(f"Created Inbound Delivery Notification {notification_id} in SAP (SOAP, references PO {po_number} directly)")
    except Exception as e:
        # Aug 2026 correction: SAP does NOT actually reject a duplicate
        # create (confirmed live - see the confirmation-check above,
        # which is the real safety net now). This branch is kept only
        # in case some future SAP config change ever DOES start
        # rejecting a genuine duplicate with an "already exist"-style
        # message - if it ever fires, treat it the same way (already
        # done, keep going) rather than failing the whole retry.
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
        # Sep 13 2026 BUG FOUND + FIXED (real incident, shipment WFRRSL/
        # PO 29482, confirmed live by the user directly in SAP: Delivery
        # 53117, Delivery Status "Finished", Release Status "Released",
        # every line's Fulfilled Quantity already matched its Delivery
        # Notification Quantity) - "disabled" here does NOT always mean
        # a corrupted/stuck document needing SAP Basis. It ALSO means
        # "this notification was already fully received in SAP" - a
        # Retry re-derives the SAME notification_id (see the Sep 12 fix
        # above) and finds a document a PREVIOUS attempt already
        # completed end-to-end (real SAP save succeeded), but THIS
        # app's own sap_gr_result never got updated to "posted" because
        # that earlier attempt crashed/restarted right after the save.
        # Read the selected row's own text for "Finished" before
        # concluding it's actually stuck - if so, treat as success
        # instead of failing forever on every future retry too.
        row_text = (await rows[0].inner_text()).lower()
        # Sep 14 2026 CORRECTED fix (real incident, PO 29482/notification
        # TEST_0506-WFRRSL-29482, confirmed by the user's own live
        # screenshot): "Delivery Status" is never one of the list's own
        # columns - it only renders in the "Details: Delivery
        # Notification ..." panel already shown BELOW the list the
        # instant `_click_po_row` selected this row above, no extra
        # click/navigation needed. See `_is_notification_finished_in_details_panel`'s
        # docstring for why the first (wrong) attempt at this fix tried
        # clicking a non-existent ID link instead.
        already_finished = "finish" in row_text or await _is_notification_finished_in_details_panel(page)
        if already_finished:
            events.append(f"Notification {notification_id} was already fully received in SAP (Delivery Status: Finished) from a previous attempt - treating as posted")
            return {"po_number": po_number, "status": "posted", "inbound_delivery_id": None, "skipped_items": skipped_items, "events": events}
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
    inbound_delivery_id = _extract_inbound_delivery_id(confirmation_text)

    # Sep 16 2026 BUG FOUND + FIXED (real incident, user's own screenshot:
    # PO 29284/notification TP/26-27/377-WFJEZ2-29284 - app reported
    # "posted", SAP Inbound Delivery # stayed blank, and the user
    # confirmed directly in SAP the Goods Receipt was NEVER actually
    # done). Root cause: this used to declare "posted" purely because NO
    # error toast was found - but absence of a visible error is NOT
    # proof of success (the toast can disappear before this runs, the
    # save can silently no-op on a slow/flaky tenant, etc). Now does a
    # REAL positive check against SAP's own confirmation report (the
    # SAME one already used for the pre-check/dedupe above) with a short
    # retry for its indexing lag (same pattern as the search-retry above)
    # before ever claiming success - a Goods Receipt that SAP itself
    # can't yet confirm is reported as a FAILURE (Retry-able), never as
    # "posted", no matter how quiet the UI was.
    confirmed_rows = []
    for attempt in range(4):
        try:
            confirmed_rows = await asyncio.to_thread(confirmation_report_client.find_confirmation_rows, po_number, notification_id)
        except Exception as e:
            events.append(f"Could not verify the Goods Receipt against SAP's own confirmation report (attempt {attempt + 1}/4): {e}")
        if confirmed_rows:
            break
        await page.wait_for_timeout(4000)
    if not confirmed_rows:
        result = await _capture_failure(
            page, po_number, "saving",
            "SAP's UI showed no error after Save and Close, but SAP's own confirmation report still shows no confirmed Goods Receipt for this PO - it may not have actually posted. Please verify in SAP and Retry.",
            events=events,
        )
        return result
    if not inbound_delivery_id:
        # Sep 16 2026 fix - same "latest wins" resolution now used by
        # check_manual_gr_quantities/fetch_inbound_delivery_ids_from_sap:
        # this report can carry stale/orphaned CDELIVERY_UUIDs from
        # earlier abandoned attempts sharing the same notification/bill
        # reference, so blindly taking the FIRST row risked surfacing a
        # stale ID instead of the real one just posted.
        delivery_ids = [r.get("CDELIVERY_UUID") for r in confirmed_rows if r.get("CDELIVERY_UUID")]
        if delivery_ids:
            try:
                inbound_delivery_id = max(delivery_ids, key=int)
            except (TypeError, ValueError):
                inbound_delivery_id = sorted(delivery_ids)[-1]
    events.append(f"SAP's own confirmation report confirms the Goods Receipt was posted (Inbound Delivery {inbound_delivery_id or 'ID pending'})")
    return {"po_number": po_number, "status": "posted", "inbound_delivery_id": inbound_delivery_id, "skipped_items": skipped_items, "events": events}


STEPS_PER_PO = 4


async def post_goods_receipt_via_ui(po_items: dict, notification_client, confirmation_report_client, progress_cb=None) -> dict:
    """po_items: {po_number: {"doc_code": str, "supplier_doc_num": str,
    "bill_date": str, "vendor_code": str, "item_qtys": {item_number:
    qty}, "item_products": {item_number: product_id}, "item_uoms":
    {item_number: unit_code}}} - one call per distinct PO, grouping every line item of that PO into
    a single submission (user's explicit ask, Aug 29 2026), matching
    the manual flow exactly. `notification_client` is a
    SAPInboundDeliveryNotificationClient (see that module) - the SOAP
    create step run inside `_post_one_po` before any browser
    navigation. `confirmation_report_client` is a
    SAPInboundDeliveryReportClient (Aug 2026, real duplicate-Goods-
    Receipt incident) - `_post_one_po` queries it first to check
    whether this exact notification_id+PO already has a real confirmed
    Goods Receipt in SAP before creating anything, since SAP itself
    never rejects a duplicate notification create. `item_products`/
    `item_uoms` feed both the SOAP create call and the Actual Quantity
    grid match by Product ID (see _fill_line_actual_quantities'
    docstring for why row-order matching alone is not safe to trust).

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
    "skipped", "error"?, "inbound_delivery_id"?, "skipped_items"?:
    [{"item_number", "reason"}]}], "total_steps"}. `skipped_items` can be
    non-empty even on a "posted" PO (Sep 13 2026) - see _post_one_po's
    missing_products handling: only the bad line(s) are dropped, the
    rest of the PO still posts."""
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
                            spec.get("vendor_code"), notification_client, confirmation_report_client, on_step=on_step, events=events,
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


async def create_inbound_delivery_notifications_only(po_items: dict, notification_client) -> list:
    """Sep 17 2026, Manual GRN (No-Playwright) path - user's explicit ask
    to eliminate Playwright flakiness for staff who'd rather post the
    actual Goods Receipt themselves directly in SAP. Runs ONLY the SOAP
    Inbound Delivery Notification create step already proven inside
    `_post_one_po` above (identical `notification_id` derivation, so the
    exact same notification a staff member sees in SAP's own "Inbound
    Delivery Notifications" list is what gets quantity-checked later) -
    no browser, no Post Goods Receipt/Actual Quantity/Save-and-Close
    automation at all. `supplier_shipment_service.check_manual_gr_quantities`
    later verifies what staff actually entered in SAP against what this
    shipment claims was shipped.

    `po_items` is the same shape `post_goods_receipt_via_ui` takes (see its
    own docstring). Returns [{"po_number", "notification_id", "status":
    "notification_created"|"failed", "error"?}] - one entry per PO, never
    raises (a single PO's SAP hiccup can't block the others)."""
    results = []
    for po_number, spec in po_items.items():
        item_qtys = dict(spec.get("item_qtys") or {})
        item_products = spec.get("item_products") or {}
        item_uoms = spec.get("item_uoms") or {}
        doc_code = spec.get("doc_code")
        supplier_doc_num = spec.get("supplier_doc_num")
        bill_date = spec.get("bill_date")
        vendor_code = spec.get("vendor_code")
        delivery_date = (bill_date or datetime.now(timezone.utc).strftime("%Y-%m-%d"))[:10]
        notification_id = _build_notification_id(supplier_doc_num, doc_code, po_number)
        missing_products = [n for n in item_qtys if not item_products.get(n)]
        if missing_products:
            item_qtys = {k: v for k, v in item_qtys.items() if k not in missing_products}
        if not item_qtys:
            results.append({
                "po_number": po_number, "notification_id": None, "status": "failed",
                "error": f"Cannot create Inbound Delivery Notification for PO {po_number} - Product ID missing in cached PO data for item(s) {', '.join(missing_products)}",
            })
            continue
        soap_items = [
            {"item_number": n, "quantity": q, "unit_of_measure": item_uoms.get(n) or "EA", "product_id": item_products.get(n)}
            for n, q in item_qtys.items()
        ]
        try:
            await asyncio.to_thread(notification_client.maintain_bundle, notification_id, po_number, vendor_code, delivery_date, soap_items, False)
            results.append({"po_number": po_number, "notification_id": notification_id, "status": "notification_created"})
        except Exception as e:
            if "already exist" in str(e).lower():
                results.append({"po_number": po_number, "notification_id": notification_id, "status": "notification_created"})
            else:
                results.append({
                    "po_number": po_number, "notification_id": notification_id, "status": "failed",
                    "error": f"Could not create the Inbound Delivery Notification in SAP for PO {po_number}: {e}",
                })
    return results


async def _confirm_put_away_task(site_id: str, po_number: str, query_client, manage_client, events: list) -> bool:
    """Sep 19 2026 fix - real user bug report + live screenshot showing
    SAP's own "Inbound Warehouse Request" Execution Details: Planned
    Quantity was always correct, but "Product Fulfilled Quantity"
    stayed 0 forever, and the Document Flow showed the Warehouse
    Order/Inbound Delivery/Warehouse Task ("Put Away") all stuck "Not
    Started". Root cause: `maintain_bundle(release=True)` above only
    creates+releases the Inbound Delivery Notification - SAP's EM1
    "one-step receiving" Logistics Model then auto-generates that whole
    downstream chain, but nothing in this app ever CONFIRMED the actual
    Put Away Warehouse Task, which is the one thing that sets Fulfilled
    Quantity. That confirm is a SEPARATE SAP object/service
    (`ManageSiteLogisticsTaskIn.MaintainBundle_V1`, sap_site_logistics_
    client.py) - live-verified this session (real task 69040, PO 29685,
    site P8: SAP returned SeverityCode "S"/"Saved Successfully" and the
    task then dropped out of the open-tasks query, confirming success).

    Looks up this PO's own Put Away task (OperationTypeCode="11") on
    `site_id` and confirms every line's ActualQuantity = PlanQuantity -
    safe to trust PlanQuantity here (unlike a task nobody has physically
    checked yet) because this whole function only ever runs AFTER
    `maintain_bundle(release=True)` has already told SAP the goods were
    received in this exact quantity. Retries a few times for SAP's own
    indexing lag between the release call and the task actually
    appearing in this query (same pattern as the confirmation-report
    retries elsewhere in this file). Never raises - the Goods Receipt
    itself already posted by the time this runs, so a failure here is a
    logged/degraded outcome (caller surfaces it via `events`), not a
    reason to fail the whole PO."""
    task = None
    for attempt in range(4):
        try:
            tasks = await asyncio.to_thread(query_client.find_tasks_for_site, site_id)
            task = next((t for t in tasks if t.get("po_number") == po_number and t.get("operation_type_code") == "11"), None)
        except Exception as e:
            events.append(f"Could not query SAP for PO {po_number}'s Put Away task (attempt {attempt + 1}/4): {e}")
        if task:
            break
        await asyncio.sleep(4)
    if not task:
        events.append(f"Could not find a Put Away task in SAP for PO {po_number} - Fulfilled Quantity may need to be confirmed manually in SAP")
        return False
    task_payload = {
        "task_id": task["task_id"], "task_uuid": task["task_uuid"],
        "referenced_object_uuid": task["referenced_object_uuid"], "operation_activity_uuid": task["operation_activity_uuid"],
        "material_inputs": [
            {"uuid": mi["uuid"], "product_id": mi["product_id"], "actual_quantity": mi["plan_quantity"], "unit_code": mi["unit_code"]}
            for mi in task.get("material_inputs", []) if mi.get("plan_quantity") is not None
        ],
        "material_outputs": [
            {"uuid": mo["uuid"], "product_id": mo["product_id"], "actual_quantity": mo["plan_quantity"], "unit_code": mo["unit_code"], "target_area": mo["target_area"]}
            for mo in task.get("material_outputs", []) if mo.get("plan_quantity") is not None
        ],
    }
    try:
        await asyncio.to_thread(manage_client.confirm_tasks_bundle, [task_payload])
        events.append(f"Confirmed Put Away Task {task['task_id']} for PO {po_number} in SAP - Fulfilled Quantity now matches Planned Quantity")
        return True
    except Exception as e:
        events.append(f"Could not confirm Put Away Task {task['task_id']} for PO {po_number} in SAP - Fulfilled Quantity may need to be confirmed manually: {e}")
        return False


async def create_and_release_inbound_delivery_notifications(po_items: dict, notification_client, confirmation_report_client) -> list:
    """Sep 18 2026, user's explicit ask: a SEPARATE "Test Full Automated
    GRN" button/path for the EM1 breakthrough (see /app/memory/
    SOAP_GRN_BREAKTHROUGH_2026-09-18.md) - identical to
    `create_inbound_delivery_notifications_only` above EXCEPT
    `release=True`, so SAP creates AND posts the real Goods Receipt with
    the ACTUAL quantities in the SAME call - no manual "Post Goods
    Receipt" SAP UI step needed at all, no Playwright anywhere. Only
    works on sites with the required EM1 Logistics Model set up in SAP
    (P8 only as of this date - server.py enforces the site allowlist
    before this is ever called).

    Same idempotency safety as `_post_one_po`'s pre-check above (this IS
    a real, irreversible SAP write, unlike the notification-only path) -
    if SAP's own confirmation report already shows a real Goods Receipt
    for this exact notification_id+PO, skip re-posting rather than risk
    a duplicate receipt on a retry.

    Sep 18 2026 correction: briefly required this SAME confirmation
    report to show a row before ever reporting "posted" (to catch a
    silently-disabled Release), but reverted it - real incident, shipment
    S000007/PO 29685: SAP's own UI confirmed the delivery was genuinely
    Released + Received, yet this analytics report (see sap_inbound_
    delivery_report_client.py's own docstring re: known field/extraction
    gaps in this exact report) still showed zero rows for it even much
    later. That confirmation report lags/gaps too unpredictably to gate
    success on - trusting maintain_bundle's own error+ID check (like
    every other path in this file) is the correct signal here.

    Sep 19 2026 - Put Away Task confirmation (`_confirm_put_away_task`)
    and the Inbound Delivery ID lookup were both tried INLINE here at
    first, but real user testing showed a fresh 2-line PO went from a
    few seconds to ~60s: each individual SAP query in this tenant costs
    4-5s+ on its own (live-measured), and the retry loops needed for
    SAP's genuine indexing lag (both the Put Away task and the
    confirmation report can take well over a minute to appear) multiplied
    that many times over, all BLOCKING the user-facing job. Moved both to
    server.py's `_auto_finish_full_auto_grn` background task (same
    "eventually consistent, not blocking" pattern the manual "Fetch from
    SAP" button already established) - this function now does ONLY the
    one real SAP write (create+release) and returns as fast as SAP allows.

    Returns [{"po_number", "notification_id", "status": "posted"|"failed",
    "error"?}] - "posted" (not "notification_created") since this status
    feeds straight into `supplier_shipment_service.finalize_goods_receipt`,
    which expects the same shape Playwright's `post_goods_receipt_via_ui`
    produces. `inbound_delivery_id`/Put Away confirmation are filled in
    later, in the background, by server.py."""
    results = []
    for po_number, spec in po_items.items():
        item_qtys = dict(spec.get("item_qtys") or {})
        item_products = spec.get("item_products") or {}
        item_uoms = spec.get("item_uoms") or {}
        doc_code = spec.get("doc_code")
        supplier_doc_num = spec.get("supplier_doc_num")
        bill_date = spec.get("bill_date")
        vendor_code = spec.get("vendor_code")
        delivery_date = (bill_date or datetime.now(timezone.utc).strftime("%Y-%m-%d"))[:10]
        notification_id = _build_notification_id(supplier_doc_num, doc_code, po_number)
        try:
            already_confirmed = await asyncio.to_thread(confirmation_report_client.find_confirmation_rows, po_number, notification_id)
        except Exception as e:
            already_confirmed = []
            logger.warning(f"Full-auto GRN: could not pre-check SAP for an existing confirmation on PO {po_number} (continuing): {e}")
        if already_confirmed:
            results.append({"po_number": po_number, "notification_id": notification_id, "status": "posted"})
            continue
        missing_products = [n for n in item_qtys if not item_products.get(n)]
        if missing_products:
            item_qtys = {k: v for k, v in item_qtys.items() if k not in missing_products}
        if not item_qtys:
            results.append({
                "po_number": po_number, "notification_id": notification_id, "status": "failed",
                "error": f"Cannot post Goods Receipt for PO {po_number} - Product ID missing in cached PO data for item(s) {', '.join(missing_products)}",
            })
            continue
        soap_items = [
            {"item_number": n, "quantity": q, "unit_of_measure": item_uoms.get(n) or "EA", "product_id": item_products.get(n)}
            for n, q in item_qtys.items()
        ]
        try:
            await asyncio.to_thread(notification_client.maintain_bundle, notification_id, po_number, vendor_code, delivery_date, soap_items, True)
            results.append({"po_number": po_number, "notification_id": notification_id, "status": "posted"})
        except Exception as e:
            results.append({
                "po_number": po_number, "notification_id": notification_id, "status": "failed",
                "error": f"SAP rejected the full automated Goods Receipt for PO {po_number}: {e}",
            })
    return results




# Sep 2 2026: the Playwright-based `fetch_open_po_quantities` that used
# to live here was CANCELLED per user's explicit ask ("we cannot go with
# playwright for this") - it was live-hammering SAP's UI sequentially
# for every active PO (10-20s each) with a ~100% failure rate (SAPUI5's
# view-selector auto-closing on a re-click of an already-selected
# value), and was found starving other concurrent SAP calls. Replaced
# with `sap_po_analytics_client.py`'s single fast OData analytics query
# - see that module's docstring.
