"""Confirm the final recorded quantity on the now-Finished notification's
own Line Items tab - direct proof the qty landed correctly in the GR."""
import asyncio
import os
import sys
from dotenv import load_dotenv

load_dotenv("/app/backend/.env")
sys.path.insert(0, "/app/backend")

from playwright.async_api import async_playwright  # noqa: E402
import sap_playwright_pgr_service as pgr  # noqa: E402


async def main():
    username = os.environ["SAP_USERNAME"]
    password = os.environ["SAP_PASSWORD"]
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1600, "height": 900})
        await pgr._login(page, username, password)
        await pgr._open_inbound_delivery_notifications(page)
        combo = page.locator('.sapMSelect, [role="combobox"]').first
        await combo.click(force=True)
        await page.wait_for_timeout(1000)
        for o in await page.query_selector_all('li, [role="option"]'):
            if (await o.inner_text()).strip() == "All Delivery Notifications by Selection":
                await o.click(force=True)
                await page.wait_for_timeout(3000)
                break
        await pgr._search_delivery(page, "EMGC0912114139")
        rows = await page.query_selector_all('tr[id^="__table"]')
        cells = await rows[0].query_selector_all("td")
        await cells[0].click(force=True)
        await page.wait_for_timeout(2000)
        line_items_tab = page.get_by_text("Line Items", exact=True).first
        await line_items_tab.click(force=True)
        await page.wait_for_timeout(2500)
        await page.screenshot(path="/app/backend/playwright_debug/idn_line_items_final.png")
        page_text = await page.inner_text("body")
        print(page_text[:3000])
        await browser.close()


asyncio.run(main())
