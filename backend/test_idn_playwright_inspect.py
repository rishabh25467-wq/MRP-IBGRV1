"""One-off: open our SOAP-created test notification EMGC0912114139 in the
Inbound Delivery Notifications view and inspect whether Actual Quantity /
Delivery Notification ID / Date came through pre-filled from the SOAP
create - reusing the EXISTING STO Playwright helpers (same underlying BO)."""
import asyncio
import os
import sys
from dotenv import load_dotenv

load_dotenv("/app/backend/.env")
sys.path.insert(0, "/app/backend")

from playwright.async_api import async_playwright  # noqa: E402
import sap_playwright_pgr_service as pgr  # noqa: E402
import sap_playwright_supplier_pgr_service as supplier_pgr  # noqa: E402


async def main():
    username = os.environ["SAP_USERNAME"]
    password = os.environ["SAP_PASSWORD"]
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1600, "height": 900})
        await pgr._login(page, username, password)
        await pgr._open_inbound_delivery_notifications(page)
        # Switch the base view dropdown from "Advised Delivery Notifications"
        # to something broader (e.g. "All Inbound Delivery Notifications")
        # since our SOAP-created doc might not carry "Advised" status.
        view_dropdown = page.locator('[aria-label="View"]').first
        combo = page.locator('.sapMSelect, [role="combobox"]').first
        print("COMBO COUNT:", await combo.count())
        if await combo.count() > 0:
            await combo.click(force=True)
            await page.wait_for_timeout(1000)
            await page.screenshot(path="/app/backend/playwright_debug/idn_view_dropdown_open.png")
            options = await page.query_selector_all('li, [role="option"]')
            for o in options:
                text = (await o.inner_text()).strip()
                if text:
                    print("VIEW OPTION:", text)
                if text == "All Delivery Notifications by Selection":
                    await o.click(force=True)
                    await page.wait_for_timeout(3000)
        hits = await pgr._search_delivery(page, "EMGC0912114139")
        print("SEARCH HITS:", hits)
        rows = await page.query_selector_all('tr[id^="__table"]')
        for row in rows:
            cells = await row.query_selector_all("td")
            texts = [(await c.inner_text()).strip() for c in cells]
            print("ROW:", texts)
        os.makedirs("/app/backend/playwright_debug", exist_ok=True)
        await page.screenshot(path="/app/backend/playwright_debug/idn_search_result.png")

        if hits > 0:
            cells = await rows[0].query_selector_all("td")
            await cells[0].click(force=True)
            await page.wait_for_timeout(1500)
            await page.screenshot(path="/app/backend/playwright_debug/idn_detail.png")
            click_result = await pgr._click_button(page, "Post Goods Receipt")
            print("POST GOODS RECEIPT CLICK:", click_result)
            await page.wait_for_timeout(4000)
            await page.screenshot(path="/app/backend/playwright_debug/idn_pgr_dialog.png")
            for row in await page.query_selector_all('tr[id^="__table"]'):
                cells = await row.query_selector_all("td")
                texts = [(await c.inner_text()).strip() for c in cells]
                print("PGR GRID ROW (before propose):", texts)

            propose_result = await pgr._click_button(page, "Propose Quantities")
            print("PROPOSE QUANTITIES CLICK:", propose_result)
            await page.wait_for_timeout(2000)

            unfilled = await supplier_pgr._fill_line_actual_quantities(
                page, item_products={"1": "WAS8PZ"}, item_qtys={"1": 1}, po_number="29482",
            )
            print("UNFILLED:", unfilled)
            for row in await page.query_selector_all('tr[id^="__table"]'):
                cells = await row.query_selector_all("td")
                texts = [(await c.inner_text()).strip() for c in cells]
                print("PGR GRID ROW (after fill):", texts)
            await page.screenshot(path="/app/backend/playwright_debug/idn_after_propose.png")

            save_result = await pgr._click_button(page, "Save and Close")
            print("SAVE AND CLOSE CLICK:", save_result)
            await page.wait_for_timeout(6000)
            await page.screenshot(path="/app/backend/playwright_debug/idn_after_save.png")
            error_text = await pgr._extract_error_text(page)
            print("ERROR TEXT:", repr(error_text))
        await browser.close()


asyncio.run(main())
