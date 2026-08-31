"""Headless Playwright automation to combine a multi-line Stock Transfer
Order's Outbound Delivery into ONE SAP delivery, fill in its GST/logistics
metadata as real structured fields, and post its Goods Issue (Aug 28 2026).

WHY THIS EXISTS: exhaustive live testing (see stock_transfer_service.py's
try_post_goods_issue docstring, attempts #1-8) proved every SAP OData/SOAP
API path - PGIInBackground (item-scoped only), SLRequestDeliveryExecution,
OutboundDeliveryRequestAllocate, SLPGIInBackground, and even the
PartialDeliveryControlCode field itself (SAP's own documented "3" for
combined full-quantity delivery, re-tested live, order 30433) - is a dead
end: this tenant's Ship-to Party Account Master Data silently forces one
Outbound Delivery per line no matter what any of these calls send.
Similarly, vehicle no/transport mode/GR no/place+date of supply are real
SAP extension fields (VehicleNo_KUT etc. on OutboundDeliveryCollection),
but a direct API write to them was already tested live and rejected - SAP
locks the whole document read-only for API writes the instant its own
scheduler picks it up (see sap_sto_client.py's _build_gst_note_text
docstring) - so this app has only ever written them as a plain text Note.

CONFIRMED LIVE (Aug 28 2026) that SAP's own UI has genuine paths for both:
1. Outbound Logistics > Delivery Proposals already lists ONE row per
   multi-line order (the header-level Delivery Request already correctly
   combines every line - that part of the Aug 27 2026 API fix does work).
   Selecting that row and clicking "Create Outbound Delivery" > "Without
   Release" creates exactly ONE combined Outbound Delivery (confirmed
   live, order 30434/30435/30441 - always 1 delivery for 2 lines).
2. Outbound Logistics > Outbound Deliveries > open that Delivery > Edit >
   View All exposes Vehicle No./Transportation Mode/Place Of Supply/G.R
   No./Date Of Supply/G.R Date as real editable fields (confirmed live,
   order 30441/delivery P8D1-203 - all 5 saved with Consistency Status
   staying "Consistent"). "Freight Forwarder" on this same screen needs a
   real Business Partner lookup, not free text - typing a plain name into
   it broke Consistency Status and disabled Release entirely (confirmed
   live) - so it is deliberately skipped here and stays a Note-only field.
   Clicking "Release" on this screen both releases AND posts Goods Issue
   in one action (confirmed live - order_fulfilment_status flipped to "3"
   Finished for both lines right after, no separate GI click needed).

Reuses the login/blocking-layer helpers from sap_playwright_pgr_service.py
(same "itadmin" business user, same session-cleanup behaviour) - only the
navigation/action targets differ (Outbound Logistics work center instead
of Inbound Logistics)."""
import logging
import os
import glob
import time
from datetime import date

from sap_playwright_pgr_service import _login, _wait_for_blocking_layer_clear, _extract_error_text, _humanize_error

logger = logging.getLogger(__name__)

DEBUG_SCREENSHOT_DIR = "/app/backend/playwright_debug"
MAX_DEBUG_SCREENSHOTS_PER_ORDER = 20


class SAPPlaywrightOutboundGIError(Exception):
    pass


async def _open_work_center_item(page, work_center: str, item: str) -> None:
    await _wait_for_blocking_layer_clear(page)
    target = page.locator(f'[aria-label="{item}"]').first
    if await target.count() == 0 or not await target.is_visible():
        await page.locator('[aria-label="Show / hide work center navigation"]').first.click(force=True, timeout=15000)
        await page.wait_for_timeout(9000)
        await page.locator(f'[aria-label="{work_center}"]').first.click(force=True, timeout=15000)
        await page.wait_for_timeout(3000)
        await _wait_for_blocking_layer_clear(page)
    await page.locator(f'[aria-label="{item}"]').first.click(force=True, timeout=15000)
    await page.wait_for_timeout(5000)


async def _fill_by_label(page, label_text: str, value: str, is_date: bool = False) -> bool:
    for label in await page.query_selector_all("label"):
        if (await label.inner_text()).strip() != label_text:
            continue
        lbox = await label.bounding_box()
        for inp in await page.query_selector_all("input"):
            ibox = await inp.bounding_box()
            if ibox and abs(ibox["y"] - lbox["y"]) < 6 and ibox["x"] > lbox["x"]:
                await inp.click()
                if is_date:
                    await inp.type(value, delay=40)
                    await inp.press("Tab")
                else:
                    await inp.fill(value)
                return True
        return False
    return False


async def _select_combo_box(page, label_text: str, value: str) -> bool:
    for label in await page.query_selector_all("label"):
        if (await label.inner_text()).strip() != label_text:
            continue
        lbox = await label.bounding_box()
        for inp in await page.query_selector_all("input.sapMComboBoxInner"):
            ibox = await inp.bounding_box()
            if ibox and abs(ibox["y"] - lbox["y"]) < 6:
                await inp.click()
                await inp.type(value, delay=60)
                await page.wait_for_timeout(1200)
                suggestion = page.locator(f"text={value}").last
                if await suggestion.count() == 0:
                    return False
                await suggestion.click(force=True)
                return True
        return False
    return False


async def _filter_by_reference(page, field_label: str, value: str) -> int:
    """Opens the Advanced Filter, fills the given field (e.g. "Reference
    ID" on Delivery Proposals, "Delivery ID" on Outbound Deliveries), and
    returns how many rows matched."""
    await page.locator('[aria-label="Open Advanced Filter"]').first.click(force=True, timeout=15000)
    await page.wait_for_timeout(3000)
    filled = await _fill_by_label(page, field_label, value)
    if not filled:
        raise SAPPlaywrightOutboundGIError(f"Could not find the '{field_label}' advanced filter field")
    rows = []
    for _ in range(6):
        for btn in await page.query_selector_all("button, .sapMBtnBase"):
            if await btn.is_visible() and (await btn.inner_text()).strip() == "Go":
                await btn.click(force=True)
                break
        await page.wait_for_timeout(6000)
        rows = await page.query_selector_all('tr[id^="__table"]')
        if rows:
            break
    return len(rows)


async def _click_button(page, label: str):
    for c in await page.query_selector_all(".sapMBtnBase"):
        if not await c.is_visible() or (await c.inner_text()).strip() != label:
            continue
        cls = await c.get_attribute("class") or ""
        if "sapMBtnDisabled" in cls:
            return "disabled"
        await c.click(force=True)
        return "clicked"
    return "not_found"


async def _save_debug_screenshot(page, sap_order_id: str, step: str = "failure") -> None:
    """User's explicit ask (Sep 2026, real incident Order 30529 - hung
    with nothing visible, no way to tell WHERE): used to only ever fire
    on a final failure, overwriting the same one file - useless for a
    hang that never reaches a failure branch at all. Now called at every
    phase transition too (see combine_and_post_goods_issue_via_ui/
    _open_delivery_and_release), one timestamped file per step so the
    NEXT hang shows exactly which step it was stuck on, instead of
    guessing blind - see server.py's debug-screenshots admin endpoints.
    Keeps only the last MAX_DEBUG_SCREENSHOTS_PER_ORDER per order id."""
    try:
        os.makedirs(DEBUG_SCREENSHOT_DIR, exist_ok=True)
        path = f"{DEBUG_SCREENSHOT_DIR}/outbound_gi_{sap_order_id}_{int(time.time() * 1000)}_{step}.png"
        await page.screenshot(path=path)
        existing = sorted(glob.glob(f"{DEBUG_SCREENSHOT_DIR}/outbound_gi_{sap_order_id}_*.png"))
        for stale in existing[:-MAX_DEBUG_SCREENSHOTS_PER_ORDER]:
            try:
                os.remove(stale)
            except OSError:
                pass
    except Exception as e:
        logger.warning(f"Could not save outbound GI debug screenshot ({step}) for order {sap_order_id}: {e}")


def _to_sap_date(iso_date: str) -> str:
    d = date.fromisoformat(iso_date)
    return d.strftime("%d.%m.%Y")


async def _fill_delivery_metadata(page, metadata: dict) -> None:
    """Fills the delivery's own Vehicle No./Transportation Mode/Place Of
    Supply/G.R No./Date Of Supply/G.R Date fields (confirmed live, Aug 28
    2026 - see module docstring). "Freight Forwarder" is deliberately
    skipped (needs a real Business Partner lookup, breaks Consistency
    Status as free text). Best-effort - metadata is a nice-to-have, a
    failure here must never block the actual Goods Issue."""
    for b in await page.query_selector_all(".sapMBtnBase, a.sapMLnk"):
        if await b.is_visible() and (await b.inner_text()).strip() == "Edit":
            await b.click(force=True)
            break
    await page.wait_for_timeout(5000)
    for b in await page.query_selector_all(".sapMBtnBase, a.sapMLnk"):
        if await b.is_visible() and "View All" in (await b.inner_text()):
            await b.click(force=True)
            break
    await page.wait_for_timeout(4000)

    sap_date = _to_sap_date(metadata["date_of_supply"]) if metadata.get("date_of_supply") else None
    await _fill_by_label(page, "G.R No.", metadata.get("gr_no") or "")
    await _fill_by_label(page, "Place Of Supply", metadata.get("place_of_supply") or "")
    await _fill_by_label(page, "Vehicle No.", metadata.get("vehicle_no") or "")
    if sap_date:
        await _fill_by_label(page, "Date Of Supply", sap_date, is_date=True)
        await page.wait_for_timeout(800)
        await _fill_by_label(page, "G.R Date", sap_date, is_date=True)
        await page.wait_for_timeout(800)
    if metadata.get("transportation_mode"):
        await _select_combo_box(page, "Transportation Mode", metadata["transportation_mode"])
    await page.wait_for_timeout(1000)

    for b in await page.query_selector_all(".sapMBtnBase"):
        if await b.is_visible() and (await b.inner_text()).strip() == "Save":
            await b.click(force=True)
            break
    await page.wait_for_timeout(5000)


async def _open_delivery_and_release(page, delivery_id: str, metadata: dict, sap_order_id: str) -> dict:
    """Shared by combine_and_post_goods_issue_via_ui (fresh Delivery,
    right after 'Create Outbound Delivery') and release_existing_delivery_via_ui
    (retry path for a Delivery an EARLIER attempt already created but
    never got Released) - opens the given Delivery ID in Outbound
    Deliveries, fills its metadata (best-effort), and clicks Release with
    the Check-Consistency retry loop (see that block's own comment).
    Returns {"status": "posted", "delivery_ids": [delivery_id]},
    {"status": "failed", "error": "..."}."""
    try:
        await _open_work_center_item(page, "Outbound Logistics", "Outbound Deliveries")
        dcount = await _filter_by_reference(page, "Delivery ID", delivery_id)
        await _save_debug_screenshot(page, sap_order_id, "outbound_deliveries_filtered")
        if dcount >= 1:
            link = None
            for l in await page.locator("a.sapMLnk, span.sapMLnk").all():
                if await l.is_visible() and delivery_id in (await l.inner_text()):
                    link = l
                    break
            if link:
                await link.click(force=True)
                await page.wait_for_timeout(6000)
                await _fill_delivery_metadata(page, metadata)
                await _save_debug_screenshot(page, sap_order_id, "metadata_filled")
    except Exception as e:
        # Metadata is best-effort - a failure here must not block
        # Goods Issue itself (see module/function docstring).
        logger.warning(f"Order {sap_order_id}: filling delivery metadata failed, proceeding to Release anyway: {e}")

    release_click = await _click_button(page, "Release")
    if release_click == "not_found":
        for b in await page.query_selector_all(".sapMBtnBase"):
            if await b.is_visible() and (await b.inner_text()).strip() == "Edit":
                await b.click(force=True)
                await page.wait_for_timeout(4000)
                release_click = await _click_button(page, "Release")
                break
    if release_click == "disabled":
        # Real incident fix (Order 30518/Delivery P1D1-492, Sep
        # 2026): SAP recomputes Consistency Status
        # ASYNCHRONOUSLY right after the metadata Save above -
        # reading the Release button the instant after Save can
        # catch it mid-recompute (still shows disabled) even
        # though SAP settles to "consistent" + an enabled
        # Release button just seconds later (confirmed: this
        # exact delivery showed "Outbound delivery consistent"
        # and a normal Release button when checked manually in
        # SAP right after this job reported it failed). Click
        # "Check Consistency" (the real SAP button, same one a
        # human would use) and re-check Release a few times
        # before ever concluding this is a real, unrecoverable
        # SAP-side rejection - a genuinely bad field value stays
        # disabled through every one of these retries too, so
        # this never masks a real rejection, just avoids a false
        # positive on a timing race.
        for _ in range(4):
            await page.wait_for_timeout(8000)
            for b in await page.query_selector_all(".sapMBtnBase"):
                if await b.is_visible() and (await b.inner_text()).strip() == "Check Consistency":
                    await b.click(force=True)
                    await page.wait_for_timeout(6000)
                    break
            release_click = await _click_button(page, "Release")
            if release_click != "disabled":
                break
    if release_click == "disabled":
        await _save_debug_screenshot(page, sap_order_id, "release_disabled")
        return {"status": "failed", "error": f"Delivery {delivery_id}'s 'Release' button is disabled - SAP Consistency Status check failed, needs manual SAP review"}
    await page.wait_for_timeout(15000)

    error_text = await _extract_error_text(page)
    release_status = page.locator("text=Released").first
    if await release_status.count() > 0:
        await _save_debug_screenshot(page, sap_order_id, "released_confirmed")
        return {"status": "posted", "delivery_ids": [delivery_id]}
    if error_text:
        await _save_debug_screenshot(page, sap_order_id, "release_error")
        return {"status": "failed", "error": _humanize_error(error_text)}
    await _save_debug_screenshot(page, sap_order_id, "release_unconfirmed")
    return {"status": "failed", "error": f"Delivery {delivery_id} was created but could not be confirmed Released - will retry"}


async def combine_and_post_goods_issue_via_ui(sap_order_id: str, metadata: dict, sap_outbound_delivery_client, item_uuids: list, progress_cb=None) -> dict:
    """Single order per call (one browser session) - the caller running
    one of these per STO concurrently gets true batch parallelism for
    free (separate headless Chromium instances, each with its own
    dedicated SAP login - see playwright_concurrency.py), matching the
    "combine + Goods Issue" pipeline staying independent from the
    separate Inbound Receipt step (receiving warehouse's own later
    action).

    progress_cb(phase: str, username: str = None), optional, plain sync
    callback (called from inside asyncio.run() on a worker thread, never
    the main event loop - a blocking DB write inside it is safe) -
    "opening_delivery" once logged in and about to act on the Delivery
    Proposal (also carries which of the pooled SAP logins this run
    acquired - user's explicit ask, Aug 31 2026, to show which bot
    account handled a given order now that there are 3 to pick from),
    "posting_goods_issue" right before the final Release click. User's
    explicit ask (Aug 31 2026) to stop the retry button leaving them
    "with no idea what's happening" during the up-to-a-minute Playwright
    run - unlike the OTHER 2 Playwright services' progress_cb, plain
    wording here is fine (this order's OWN GI banner already says "SAP"
    elsewhere, unlike the Inbound Receipt/Supplier GRN flows).

    Returns {"status": "waiting"} if SAP hasn't produced the combined
    Delivery Proposal yet (caller should keep polling, same as the
    existing per-line path), {"status": "posted", "delivery_ids": [...]}
    once Release is confirmed (which also posts Goods Issue - confirmed
    live), or {"status": "failed", "error": "..."} on a real SAP-side
    rejection."""
    from playwright.async_api import async_playwright
    import playwright_concurrency

    def _progress(phase: str, username: str = None) -> None:
        if progress_cb:
            try:
                progress_cb(phase, username)
            except Exception:
                pass

    username, password = await playwright_concurrency.acquire()
    try:
        async with async_playwright() as p:
            browser = await playwright_concurrency.launch_chromium(p)
            playwright_concurrency.register_browser(browser)
            try:
                page = await browser.new_page(viewport={"width": 1600, "height": 900})
                await _login(page, username, password)
                _progress("opening_delivery", username=username)
                await _save_debug_screenshot(page, sap_order_id, "after_login")
                await _open_work_center_item(page, "Outbound Logistics", "Delivery Proposals")
                row_count = await _filter_by_reference(page, "Reference ID", sap_order_id)
                await _save_debug_screenshot(page, sap_order_id, "delivery_proposals_filtered")
                if row_count == 0:
                    return {"status": "waiting"}

                rows = await page.query_selector_all('tr[id^="__table"]')
                await rows[0].click(force=True)
                await page.wait_for_timeout(500)
                for row in rows[1:]:
                    await page.keyboard.down("Control")
                    await row.click(force=True)
                    await page.keyboard.up("Control")
                    await page.wait_for_timeout(300)

                create_click = await _click_button(page, "Create Outbound Delivery")
                if create_click == "disabled":
                    await _save_debug_screenshot(page, sap_order_id, "create_delivery_disabled")
                    return {"status": "failed", "error": "'Create Outbound Delivery' is disabled for this order in SAP - needs manual SAP review"}
                if create_click == "not_found":
                    await _save_debug_screenshot(page, sap_order_id, "create_delivery_not_found")
                    return {"status": "failed", "error": "'Create Outbound Delivery' button not found on the Delivery Proposals screen"}
                await page.wait_for_timeout(2000)
                without_release = page.locator("text=Without Release").first
                if await without_release.count() == 0:
                    await _save_debug_screenshot(page, sap_order_id, "without_release_not_found")
                    return {"status": "failed", "error": "'Without Release' option not found after clicking 'Create Outbound Delivery'"}
                await without_release.click(force=True)
                await page.wait_for_timeout(15000)
                await _save_debug_screenshot(page, sap_order_id, "delivery_created")

                delivery_ids = []
                for _ in range(4):
                    try:
                        objects = sap_outbound_delivery_client.find_outbound_delivery_objects(item_uuids)
                    except Exception as e:
                        logger.warning(f"Order {sap_order_id}: delivery ID lookup after 'Create Outbound Delivery' failed, retrying: {e}")
                        objects = []
                    found_uuids = {o.get("item_uuid") for o in objects if o.get("item_uuid")}
                    if set(item_uuids).issubset(found_uuids):
                        delivery_ids = sorted({o["id"] for o in objects if o.get("id")})
                        break
                    await page.wait_for_timeout(8000)
                if not delivery_ids:
                    await _save_debug_screenshot(page, sap_order_id, "delivery_id_not_found")
                    return {"status": "failed", "error": "Outbound Delivery was created but its ID could not be found via SAP OData afterward - will retry"}
                if len(delivery_ids) != 1:
                    logger.warning(f"Order {sap_order_id}: expected 1 combined delivery, SAP OData shows {len(delivery_ids)}: {delivery_ids}")

                _progress("posting_goods_issue")
                return await _open_delivery_and_release(page, delivery_ids[0], metadata, sap_order_id)
            finally:
                await browser.close()
                playwright_concurrency.unregister_browser(browser)
    finally:
        playwright_concurrency.release((username, password))


async def release_existing_delivery_via_ui(sap_order_id: str, delivery_id: str, metadata: dict, progress_cb=None) -> dict:
    """Retry path (real incident fix, Order 30518/Delivery P1D1-492, Sep
    2026): once an earlier attempt already created a real combined
    Outbound Delivery for a multi-line order, its underlying request
    items are gone from SAP's "pending" Delivery Proposals list for good
    (they've been consumed into that Delivery) - re-running
    combine_and_post_goods_issue_via_ui from scratch would just see
    `row_count == 0` on Delivery Proposals and report "waiting" forever,
    NEVER attempting to release the Delivery that already exists. Skips
    straight to Outbound Deliveries and reuses the same Release+
    Check-Consistency-retry logic. See stock_transfer_service.py's
    `_try_release_existing_multiline_delivery` - this is only reached
    there as a fallback, after a faster direct API release attempt."""
    from playwright.async_api import async_playwright
    import playwright_concurrency

    def _progress(phase: str, username: str = None) -> None:
        if progress_cb:
            try:
                progress_cb(phase, username)
            except Exception:
                pass

    username, password = await playwright_concurrency.acquire()
    try:
        async with async_playwright() as p:
            browser = await playwright_concurrency.launch_chromium(p)
            playwright_concurrency.register_browser(browser)
            try:
                page = await browser.new_page(viewport={"width": 1600, "height": 900})
                await _login(page, username, password)
                _progress("opening_delivery", username=username)
                result = await _open_delivery_and_release(page, delivery_id, metadata, sap_order_id)
                _progress("posting_goods_issue")
                return result
            finally:
                await browser.close()
                playwright_concurrency.unregister_browser(browser)
    finally:
        playwright_concurrency.release((username, password))
