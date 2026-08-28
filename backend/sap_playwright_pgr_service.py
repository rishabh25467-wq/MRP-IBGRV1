"""Headless Playwright automation to post Goods Receipt for SAP Business
ByDesign Inbound Delivery Notifications (Aug 28 2026).

WHY THIS EXISTS: exhaustive live testing (see sap_inbound_delivery_client.py)
proved SAP's own custom OData action `InboundDeliveryPGRBackground`
(PGR_BACKGROUND) is genuinely disabled for this tenant's inbound deliveries -
re-confirmed live on a completely fresh, never-touched delivery (clean HTTP
500 "action is disabled", ruling out a stale-lock artifact). The exposed
action only accepts an ObjectID (no quantity/warehouse instance data), while
SAP's own native "Post Goods Receipt" UI screen opens a full data-entry form
first - strongly suggesting the underlying SAP action needs richer input than
this tenant's custom OData wrapper exposes (flagged as a real SAP Support/
Basis ticket candidate, not fixable from this app). Also checked Site
Logistics Task (QuerySiteLogisticsTaskIn) as an alternative API path -
confirmed this tenant does NOT use task-based execution for inbound
deliveries (0 hits for ProcessTypeCode=1 Inbound even on a wildcard query),
so that path doesn't apply either. Two other custom OData services
("kh*"; SAP Cloud Application Studio exports the user found on GitHub) were
also checked - not deployable from here (need SAP Cloud Studio + a PDI
developer key) and, on inspection, expose the exact same ObjectID-only
action shape or no create/confirm action at all, so they would not help
even if deployed.

This module drives the real SAP ByDesign browser UI headlessly, using the
`SAP_USERNAME`/`SAP_PASSWORD` business user (itadmin - a UI-capable login,
distinct from the technical `SAP_SOAP_USERNAME`/`SAP_ODATA_USERNAME`
accounts used for API calls elsewhere), to click the exact same "Post Goods
Receipt" button a human would. Used for BOTH internal STO receipts
(inbound_receipt_service.py) and external Supplier Portal GRN stock items -
identical underlying SAP action, just a different set of delivery IDs.

Login handles SAP's "already logged on" interstitial (each headless run is a
brand new session) by always ticking "Delete all sessions?" before
continuing - otherwise session slots pile up run after run.

Success/failure per delivery is verified by RE-SEARCHING the same delivery
ID in the "Advised Delivery Notifications" list view after the action: that
view only ever shows Not-Released/Advised deliveries, so a delivery that
posted successfully disappears from it (0 search hits = success). Still
present = failed/blocked - any visible error dialog/banner text is captured
and the edit screen is explicitly closed (never left open/locked) before
moving to the next delivery, so a failed delivery is always left in a clean,
retriable state."""
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

VHOST = "my431827.businessbydesign.cloud.sap"
LOGIN_URL = f"https://{VHOST}/sap/public/ap/ui/runtime"
DEBUG_SCREENSHOT_DIR = "/app/backend/playwright_debug"


class SAPPlaywrightPGRError(Exception):
    pass


async def _login(page, username: str, password: str) -> None:
    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=90000)
    await page.wait_for_timeout(2500)
    text_inputs = await page.query_selector_all("input[type='text'], input:not([type])")
    pw_inputs = await page.query_selector_all("input[type='password']")
    if not text_inputs or not pw_inputs:
        raise SAPPlaywrightPGRError("SAP login page did not render as expected")
    await text_inputs[0].fill(username)
    await pw_inputs[0].fill(password)
    await page.click("text=Log On")
    await page.wait_for_timeout(5000)
    # Every headless run is a brand new session - always clear old ones so
    # session slots/object locks from previous runs never pile up.
    continue_btn = page.locator("#__control1-continueBtn")
    if await continue_btn.count() > 0:
        checkbox = page.locator(".sapMCb").first
        if await checkbox.count() > 0:
            await checkbox.click()
        await continue_btn.click()
    await page.wait_for_timeout(6000)
    if "My Launchpad" not in await page.title():
        raise SAPPlaywrightPGRError("SAP login did not reach the launchpad - check credentials/session state")


async def _wait_for_blocking_layer_clear(page) -> None:
    """SAP's own busy/modal overlay (shown while a dialog is closing or a
    screen is loading) briefly intercepts clicks - wait it out instead of
    racing it, which is what caused a hard 30s click timeout right after
    dismissing a "Save failed" error and moving on to the next delivery."""
    try:
        await page.locator("#sap-b-ui-blocklayer-popup").wait_for(state="hidden", timeout=10000)
    except Exception:
        pass


async def _open_inbound_delivery_notifications(page) -> None:
    """Re-clicking the "Inbound Logistics" nav icon while it's ALREADY the
    active work center toggles its flyout closed instead of opening it -
    that's what caused "Inbound Delivery Notifications" to time out on the
    2nd+ delivery in a batch. Only click it if the submenu item isn't
    already visible."""
    await _wait_for_blocking_layer_clear(page)
    notif_item = page.locator('[aria-label="Inbound Delivery Notifications"]').first
    if await notif_item.count() == 0 or not await notif_item.is_visible():
        await page.locator('[aria-label="Inbound Logistics"]').first.click(force=True, timeout=15000)
        await page.wait_for_timeout(4000)
        await _wait_for_blocking_layer_clear(page)
    await page.locator('[aria-label="Inbound Delivery Notifications"]').first.click(force=True, timeout=15000)
    await page.wait_for_timeout(5000)


async def _search_delivery(page, delivery_id: str) -> int:
    """Types delivery_id into the list's own search box and returns how
    many DATA ROWS matched (0 once a delivery has posted and left the
    Advised/Not-Released default view). Matches only the list's own table
    rows (id starts with "__table") - a plain text search would also
    match the detail panel below, which repeats the ID as label text and
    never actually reaches 0."""
    search_inputs = await page.query_selector_all("input.sapMSFI")
    if not search_inputs:
        raise SAPPlaywrightPGRError("Could not find the Inbound Delivery Notifications search box")
    target = search_inputs[-1]
    await target.fill("")
    await target.fill(delivery_id)
    await target.press("Enter")
    await page.wait_for_timeout(3500)
    rows = await page.query_selector_all('tr[id^="__table"]')
    return len(rows)


async def _click_button(page, label: str) -> str:
    """Returns "clicked", "disabled", or "not_found". SAP's list-screen
    toolbar buttons render as `.sapMBtnBase` DIVs (disabled ones get an
    extra `sapMBtnDisabled` class - there's no real `disabled` attribute
    to check), while Fiori-elements sub-screens (e.g. the GR review tab)
    use native `<button>` elements instead - both are checked here."""
    for c in await page.query_selector_all(".sapMBtnBase"):
        if not await c.is_visible():
            continue
        if (await c.inner_text()).strip() != label:
            continue
        cls = await c.get_attribute("class") or ""
        if "sapMBtnDisabled" in cls:
            return "disabled"
        await c.click()
        return "clicked"
    for b in await page.query_selector_all("button"):
        if not await b.is_visible():
            continue
        if (await b.inner_text()).strip() != label:
            continue
        if await b.get_attribute("disabled") is not None:
            return "disabled"
        await b.click()
        return "clicked"
    return "not_found"


async def _extract_error_text(page) -> str:
    """Only "Message Strip Error" counts as a failure - the same
    role='alert' region also carries success confirmations ("Inbound
    delivery created", "Your entries have been saved"), which must not
    be mistaken for an error."""
    for sel in ["[role='alert']", "[role='alertdialog']", ".sapMMessageToast", ".sapMDialog"]:
        el = page.locator(sel).first
        if await el.count() > 0 and await el.is_visible():
            text = (await el.inner_text()).strip()
            if text and "Error" in text:
                return text[:300]
    return ""


async def _save_debug_screenshot(page, delivery_id: str) -> None:
    try:
        os.makedirs(DEBUG_SCREENSHOT_DIR, exist_ok=True)
        await page.screenshot(path=f"{DEBUG_SCREENSHOT_DIR}/{delivery_id}.png")
    except Exception as e:
        logger.warning(f"Could not save PGR debug screenshot for {delivery_id}: {e}")


async def _set_line_actual_quantity(page, product_id: str, qty: float) -> bool:
    """Types directly into the "Actual Quantity" grid cell for the row
    matching product_id (column position confirmed live: Product ID is
    the 5th <td>, Actual Quantity the 8th - Planned Quantity/Target
    Logistics Area/Identified Stock ID sit around it). Used for a
    quantity override (a value different from the full shipped amount);
    "Propose Quantities" already fills every OTHER row with its full
    Planned Quantity by default, so this only needs to run for the
    lines the user actually edited."""
    for row in await page.query_selector_all('tr[id^="__table"]'):
        cells = await row.query_selector_all("td")
        if len(cells) < 8:
            continue
        if (await cells[4].inner_text()).strip() != product_id:
            continue
        actual_qty_input = await cells[7].query_selector("input")
        if not actual_qty_input:
            return False
        await actual_qty_input.fill("")
        await actual_qty_input.fill(str(qty))
        await actual_qty_input.press("Tab")
        return True
    return False


async def _post_one_delivery(page, delivery_id: str, line_overrides: dict = None) -> dict:
    # Fresh nav every time - guarantees a known clean state regardless of
    # what the previous delivery in this batch left behind.
    await _open_inbound_delivery_notifications(page)
    before_hits = await _search_delivery(page, delivery_id)
    if before_hits == 0:
        # Cannot be trusted as "received" - this custom OData service has
        # no status field to verify against, and this tenant is a live
        # production system other people also use, so it's genuinely
        # ambiguous whether someone else already posted it or the search
        # just missed it. Report distinctly rather than claiming success.
        return {"delivery_id": delivery_id, "status": "skipped", "error": "Not found in the Advised list - may already be posted in SAP, or removed. Please verify directly in SAP."}

    click_result = await _click_button(page, "Post Goods Receipt")
    if click_result == "disabled":
        return {"delivery_id": delivery_id, "status": "failed", "error": "Post Goods Receipt is disabled for this delivery in SAP (likely corrupted from an earlier action) - needs manual SAP Basis intervention"}
    if click_result == "not_found":
        return {"delivery_id": delivery_id, "status": "failed", "error": "Post Goods Receipt button not found for this delivery"}
    await page.wait_for_timeout(6000)

    # "Actual Quantity" per line is mandatory but never auto-fills from
    # "Planned Quantity" - "Propose Quantities" copies Planned -> Actual
    # for every line in one click, exactly matching this feature's
    # default "receive the full shipped quantity" behaviour. Any line
    # the user explicitly overrode then gets typed directly into its own
    # Actual Quantity grid cell, overwriting that default - if that typing
    # fails, this MUST fail loudly rather than silently post the full
    # (wrong) quantity and report success.
    await _click_button(page, "Propose Quantities")
    await page.wait_for_timeout(2000)
    grid_product_ids = set()
    for row in await page.query_selector_all('tr[id^="__table"]'):
        cells = await row.query_selector_all("td")
        if len(cells) > 4:
            grid_product_ids.add((await cells[4].inner_text()).strip())
    for product_id, qty in (line_overrides or {}).items():
        if product_id not in grid_product_ids:
            continue  # this delivery doesn't carry that product - nothing to override here
        if not await _set_line_actual_quantity(page, product_id, qty):
            await _save_debug_screenshot(page, delivery_id)
            await _click_button(page, "Close")
            return {"delivery_id": delivery_id, "status": "failed", "error": f"Could not enter the overridden quantity for {product_id} - its Actual Quantity cell wasn't found"}

    if await _click_button(page, "Save and Close") != "clicked":
        await _save_debug_screenshot(page, delivery_id)
        await _click_button(page, "Close")
        return {"delivery_id": delivery_id, "status": "failed", "error": "Save and Close button not found"}
    await page.wait_for_timeout(6000)

    # Capture any validation error text WHILE still on this screen (it's
    # gone once we navigate away), then always leave the screen in a
    # clean, closed state before verifying via the list.
    error_text = await _extract_error_text(page)
    if error_text:
        await _save_debug_screenshot(page, delivery_id)
        await _click_button(page, "Close")
        return {"delivery_id": delivery_id, "status": "failed", "error": _humanize_error(error_text)}

    await _open_inbound_delivery_notifications(page)
    after_hits = await _search_delivery(page, delivery_id)
    if after_hits == 0:
        return {"delivery_id": delivery_id, "status": "received"}
    return {"delivery_id": delivery_id, "status": "failed", "error": "Delivery still shows as Not Released after Save and Close"}


def _humanize_error(raw: str) -> str:
    """Strips SAP's own message-strip chrome ("2 Messages / Message
    Strip Error / Save failed / Message Strip Error / <actual message>")
    down to just the real message."""
    parts = [p.strip() for p in raw.split("\n") if p.strip()]
    real = [p for p in parts if p not in ("Message Strip Error",) and "Messages" not in p and p != "Save failed"]
    return real[-1] if real else raw


async def post_goods_receipts_via_ui(username: str, password: str, delivery_ids: list, line_overrides: dict = None, progress_cb=None) -> dict:
    """Logs in ONCE, then posts Goods Receipt for every delivery_id in
    order - best-effort per delivery, one failure never stops the rest.
    `line_overrides`: optional {product_id: qty} for any line the user
    edited away from the full shipped quantity (applied wherever that
    product_id turns up, across whichever delivery actually has it - see
    inbound_receipt_service.build_line_overrides). progress_cb(step: str)
    is called before each major step so the caller can persist progress
    for a polling UI. Returns {"results": [{"delivery_id",
    "status": "received"|"failed", "error"?}]}."""
    from playwright.async_api import async_playwright

    total = len(delivery_ids)
    results = []

    def _progress(step: str) -> None:
        if progress_cb:
            try:
                progress_cb(step)
            except Exception:
                pass

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 1600, "height": 900})
            _progress("Logging into SAP...")
            await _login(page, username, password)
            for idx, delivery_id in enumerate(delivery_ids, start=1):
                _progress(f"Posting Goods Receipt for {delivery_id} ({idx}/{total})...")
                try:
                    result = await _post_one_delivery(page, delivery_id, line_overrides)
                except Exception as e:
                    logger.error(f"Playwright PGR failed for {delivery_id}: {e}")
                    await _save_debug_screenshot(page, delivery_id)
                    result = {"delivery_id": delivery_id, "status": "failed", "error": "Could not reach SAP's receipt screen - please retry"}
                results.append(result)
                # A failure can leave SAP in an unknown state (a stuck
                # dialog/lock the work-center nav can't recover from) -
                # a fresh page + re-login guarantees every remaining
                # delivery in this batch starts clean, instead of every
                # one of them cascading into a false nav-timeout failure.
                if result["status"] == "failed" and idx < total:
                    try:
                        await page.close()
                    except Exception:
                        pass
                    page = await browser.new_page(viewport={"width": 1600, "height": 900})
                    await _login(page, username, password)
        finally:
            await browser.close()

    _progress("Done")
    return {"results": results, "completed_at": datetime.now(timezone.utc).isoformat()}
