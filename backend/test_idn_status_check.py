"""Follow-up: expand the '2 Messages' strip after Save and Close, and
inspect the newly-created Inbound Delivery 53035's own status to see
if the physical Goods Receipt actually posted."""
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
        hits = await pgr._search_delivery(page, "EMGC0912114139")
        print("SEARCH HITS:", hits)
        rows = await page.query_selector_all('tr[id^="__table"]')
        if rows:
            texts = [(await c.inner_text()).strip() for c in await rows[0].query_selector_all("td")]
            print("NOTIFICATION ROW NOW:", texts)
            cells = await rows[0].query_selector_all("td")
            await cells[0].click(force=True)
            await page.wait_for_timeout(2000)
            for row in await page.query_selector_all('tr[id^="__table"]'):
                pass
            page_text = await page.inner_text("body")
            print("--- PAGE TEXT (first 2000) ---")
            print(page_text[:2000])
            await page.screenshot(path="/app/backend/playwright_debug/idn_final_detail.png")
        await browser.close()


asyncio.run(main())
