"""Read-only investigation: does the PO Detail screen's own Line Items
grid show a per-item Open/Delivered quantity column? (SOAP endpoints
here are hard-scoped to one operation each - confirmed via 415/500 on
every alternate operation tried - so this checks the SAP UI itself,
which a human buyer already relies on for the same info.)"""
import asyncio
import os

os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/pw-browsers")
from dotenv import load_dotenv
load_dotenv()

from playwright.async_api import async_playwright
import playwright_concurrency
from sap_playwright_pgr_service import _login
from sap_playwright_supplier_pgr_service import _open_purchase_orders, _search_po_exact

OUT = "/app/backend/playwright_debug/po_open_qty_investigation"
os.makedirs(OUT, exist_ok=True)


async def main():
    username, password = await playwright_concurrency.acquire()
    try:
        async with async_playwright() as p:
            browser = await playwright_concurrency.launch_chromium(p)
            playwright_concurrency.register_browser(browser)
            try:
                page = await browser.new_page(viewport={"width": 1600, "height": 900})
                await _login(page, username, password)
                await _open_purchase_orders(page)
                await _search_po_exact(page, "28792")
                rows = await page.query_selector_all('tr[id^="__table"]')
                cells = await rows[0].query_selector_all("td")
                for cell in cells:
                    link = await cell.query_selector("a")
                    if link and (await link.inner_text()).strip() == "28792":
                        await link.click(force=True)
                        break
                await page.wait_for_timeout(4000)
                await page.screenshot(path=f"{OUT}/po_detail.png")
                tab = page.get_by_text("Line Items", exact=True).first
                if await tab.count() > 0:
                    await tab.click(force=True)
                    await page.wait_for_timeout(3000)
                await page.screenshot(path=f"{OUT}/po_line_items.png")
                headers = [await h.inner_text() for h in await page.locator("th").all()]
                with open("/tmp/po_detail_headers.txt", "w") as f:
                    f.write("\n".join(headers))
            finally:
                await browser.close()
                playwright_concurrency.unregister_browser(browser)
    finally:
        playwright_concurrency.release((username, password))


asyncio.run(main())
