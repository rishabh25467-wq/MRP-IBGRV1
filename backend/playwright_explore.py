import asyncio, os, sys
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")
from playwright.async_api import async_playwright

VHOST = "my431827.businessbydesign.cloud.sap"
LOGIN_URL = f"https://{VHOST}/sap/public/ap/ui/runtime"
USER = os.environ["SAP_USERNAME"]
PASS = os.environ["SAP_PASSWORD"]

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1600, "height": 900})
        print("Navigating to", LOGIN_URL)
        await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(3000)
        await page.fill("input[name='j_username'], #logonuidfield_input, input[title='User']", USER) if False else None
        # locate by label text fallback
        inputs = await page.query_selector_all("input[type='text'], input:not([type])")
        pw_inputs = await page.query_selector_all("input[type='password']")
        print("text inputs:", len(inputs), "password inputs:", len(pw_inputs))
        if inputs:
            await inputs[0].fill(USER)
        if pw_inputs:
            await pw_inputs[0].fill(PASS)
        await page.screenshot(path="/app/backend/pw_debug_2_filled.png")
        await page.click("#LOGON_BUTTON") if False else None
        await page.click("text=Log On")
        await page.wait_for_timeout(5000)
        # Handle "already logged on" interstitial - tick delete-all-sessions then continue
        continue_btn = page.locator("#__control1-continueBtn")
        if await continue_btn.count() > 0:
            print("Session interstitial detected - deleting old sessions and continuing")
            checkbox = page.locator(".sapMCb").first
            try:
                await checkbox.click(timeout=3000)
            except Exception as e:
                print("checkbox click failed:", e)
            await continue_btn.click()
        await page.wait_for_timeout(8000)
        await page.screenshot(path="/app/backend/pw_debug_3_after_login.png")
        print("Title after login:", await page.title())
        print("URL after login:", page.url)

        # Try global search for a known Inbound Delivery Notification ID
        await page.click("[title='Search'], .lsIconTB, svg", timeout=5000) if False else None
        search_icon = page.locator("text=Search").first
        try:
            await search_icon.click(timeout=5000)
        except Exception as e:
            print("search icon click failed:", e)
        await page.wait_for_timeout(2000)
        await page.screenshot(path="/app/backend/pw_debug_4_search_open.png")
        await browser.close()

asyncio.run(main())
