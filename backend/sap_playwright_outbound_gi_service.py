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
from datetime import date

from sap_playwright_pgr_service import _login, _wait_for_blocking_layer_clear, _extract_error_text, _humanize_error

logger = logging.getLogger(__name__)

DEBUG_SCREENSHOT_DIR = "/app/backend/playwright_debug"


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


async def _save_debug_screenshot(page, sap_order_id: str) -> None:
    try:
        os.makedirs(DEBUG_SCREENSHOT_DIR, exist_ok=True)
        await page.screenshot(path=f"{DEBUG_SCREENSHOT_DIR}/outbound_gi_{sap_order_id}.png")
    except Exception as e:
        logger.warning(f"Could not save outbound GI debug screenshot for order {sap_order_id}: {e}")


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


async def combine_and_post_goods_issue_via_ui(username: str, password: str, sap_order_id: str, metadata: dict, sap_outbound_delivery_client, item_uuids: list) -> dict:
    """Single order per call (one browser session) - the caller running
    one of these per STO concurrently gets true batch parallelism for
    free (separate headless Chromium instances), matching the "combine +
    Goods Issue" pipeline staying independent from the separate Inbound
    Receipt step (receiving warehouse's own later action).

    Returns {"status": "waiting"} if SAP hasn't produced the combined
    Delivery Proposal yet (caller should keep polling, same as the
    existing per-line path), {"status": "posted", "delivery_ids": [...]}
    once Release is confirmed (which also posts Goods Issue - confirmed
    live), or {"status": "failed", "error": "..."} on a real SAP-side
    rejection."""
    from playwright.async_api import async_playwright
    import playwright_concurrency

    await playwright_concurrency.acquire()
    try:
        async with async_playwright() as p:
            browser = await playwright_concurrency.launch_chromium(p)
            playwright_concurrency.register_browser(browser)
            try:
                page = await browser.new_page(viewport={"width": 1600, "height": 900})
                await _login(page, username, password)
                await _open_work_center_item(page, "Outbound Logistics", "Delivery Proposals")
                row_count = await _filter_by_reference(page, "Reference ID", sap_order_id)
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
                    return {"status": "failed", "error": "'Create Outbound Delivery' is disabled for this order in SAP - needs manual SAP review"}
                if create_click == "not_found":
                    return {"status": "failed", "error": "'Create Outbound Delivery' button not found on the Delivery Proposals screen"}
                await page.wait_for_timeout(2000)
                without_release = page.locator("text=Without Release").first
                if await without_release.count() == 0:
                    await _save_debug_screenshot(page, sap_order_id)
                    return {"status": "failed", "error": "'Without Release' option not found after clicking 'Create Outbound Delivery'"}
                await without_release.click(force=True)
                await page.wait_for_timeout(15000)

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
                    await _save_debug_screenshot(page, sap_order_id)
                    return {"status": "failed", "error": "Outbound Delivery was created but its ID could not be found via SAP OData afterward - will retry"}
                if len(delivery_ids) != 1:
                    logger.warning(f"Order {sap_order_id}: expected 1 combined delivery, SAP OData shows {len(delivery_ids)}: {delivery_ids}")

                try:
                    await _open_work_center_item(page, "Outbound Logistics", "Outbound Deliveries")
                    dcount = await _filter_by_reference(page, "Delivery ID", delivery_ids[0])
                    if dcount >= 1:
                        link = None
                        for l in await page.locator("a.sapMLnk, span.sapMLnk").all():
                            if await l.is_visible() and delivery_ids[0] in (await l.inner_text()):
                                link = l
                                break
                        if link:
                            await link.click(force=True)
                            await page.wait_for_timeout(6000)
                            await _fill_delivery_metadata(page, metadata)
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
                    # A disabled Release button means SAP's own Consistency
                    # Status check failed (e.g. a bad field value) - this is
                    # an unrecoverable SAP-side rejection, not a transient
                    # timing issue, so it must not be retried for the full
                    # 20-min poll window.
                    await _save_debug_screenshot(page, sap_order_id)
                    return {"status": "failed", "error": f"Delivery {delivery_ids[0]}'s 'Release' button is disabled - SAP Consistency Status check failed, needs manual SAP review"}
                await page.wait_for_timeout(15000)

                error_text = await _extract_error_text(page)
                release_status = page.locator("text=Released").first
                if await release_status.count() > 0:
                    return {"status": "posted", "delivery_ids": delivery_ids}
                if error_text:
                    await _save_debug_screenshot(page, sap_order_id)
                    return {"status": "failed", "error": _humanize_error(error_text)}
                await _save_debug_screenshot(page, sap_order_id)
                return {"status": "failed", "error": f"Delivery {delivery_ids[0]} was created but could not be confirmed Released - will retry"}
            finally:
                await browser.close()
                playwright_concurrency.unregister_browser(browser)
    finally:
        playwright_concurrency.release()
