"""Check EMGR0912121735's actual status after real SOAP release=true,
now that the task-based Logistics Model is active - is GR fully posted
or is there a pending warehouse task needing separate confirmation?"""
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
        hits = await pgr._search_delivery(page, "EMGR0912121735")
        print("SEARCH HITS:", hits)
        rows = await page.query_selector_all('tr[id^="__table"]')
        if rows:
            cells = await rows[0].query_selector_all("td")
            print("ROW:", [(await c.inner_text()).strip() for c in cells])
            await cells[0].click(force=True)
            await page.wait_for_timeout(2500)
            page_text = await page.inner_text("body")
            print("--- STATUS DETAIL ---")
            print(page_text[:2200])
            await page.screenshot(path="/app/backend/playwright_debug/emgr_status.png")
        await browser.close()


asyncio.run(main())
