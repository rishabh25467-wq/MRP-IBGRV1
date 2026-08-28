"""One-off READ-ONLY investigation (Aug 28 2026): map out what SAP ByDesign
UI screen/action lets someone manually post a Goods Receipt against a real
stock Purchase Order line for an external supplier - PO 28792 (HAMIDI
EXPORTS, vendor H1330) chosen from the live Supplier Portal PO cache.

NEVER clicks Save/Post/Confirm - only navigates and captures what's on
screen (title, visible work-center nav items, buttons) as screenshots +
printed text, so the main agent can decide the right automation target
before writing anything real. Run directly: `python3 investigate_supplier_grn.py`.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv
load_dotenv()

from sap_playwright_pgr_service import _login, VHOST, _wait_for_blocking_layer_clear, _search_delivery
import playwright_concurrency

DEBUG_DIR = "/app/backend/playwright_debug/grn_investigation"
os.makedirs(DEBUG_DIR, exist_ok=True)


async def shot(page, name: str):
    path = f"{DEBUG_DIR}/{name}.png"
    await page.screenshot(path=path, full_page=False)
    print(f"[shot] {name} -> {path} | title={await page.title()}")


async def list_nav_items(page, label: str):
    items = await page.query_selector_all("[aria-label]")
    labels = set()
    for it in items:
        al = await it.get_attribute("aria-label")
        if al and await it.is_visible():
            labels.add(al)
    print(f"[{label}] visible aria-labels ({len(labels)}):")
    for l in sorted(labels):
        print(f"    - {l}")


async def main():
    from playwright.async_api import async_playwright
    username = os.environ["SAP_USERNAME"]
    password = os.environ["SAP_PASSWORD"]

    await playwright_concurrency.acquire()
    try:
        async with async_playwright() as p:
            browser = await playwright_concurrency.launch_chromium(p)
            playwright_concurrency.register_browser(browser)
            try:
                page = await browser.new_page(viewport={"width": 1600, "height": 900})
                print("Logging in...")
                await _login(page, username, password)
                await shot(page, "01_launchpad")

                # Reveal the work-center nav pane (starts collapsed).
                await page.locator('[aria-label="Show / hide work center navigation"]').first.click()
                await page.wait_for_timeout(2500)
                await shot(page, "02_workcenter_nav_open")

                # That pane defaults to "Self Service" launchpad tiles - the
                # actual list of Work Centers (Purchasing, Inbound Logistics,
                # Supplier Delivery, etc.) is behind the "Work Centers" list
                # icon at the bottom of the same left nav pane.
                await page.locator('[aria-label="Work Centers"]').first.click()
                await page.wait_for_timeout(2500)
                await shot(page, "03_workcenters_list")

                # Click through every role-tab (Self Service/GP/Cummulative/
                # Ray/Radish/Work:Inbox) and dump each tab's tile names -
                # looking for anything Purchasing/Receiving/Supplier related.
                for tab_name in ["GP", "Cummulative", "Ray", "Radish", "Work: Inbox"]:
                    tab = page.get_by_text(tab_name, exact=True).first
                    if await tab.count() == 0:
                        print(f"[tab:{tab_name}] not found")
                        continue
                    await tab.click()
                    await page.wait_for_timeout(2000)
                    await shot(page, f"04_tab_{tab_name.replace(' ', '_').replace(':', '')}")

                # Found it: left work-center sidebar shows "Goods and
                # Services Receipts" - open it and search for PO 28792.
                await page.locator("text=Goods and Services Receipts").first.click()
                await page.wait_for_timeout(3000)
                await shot(page, "05_gsr_workcenter")
                # The flyout submenu (visible in the screenshot) has
                # "Purchase Orders to Be Delivered" > "Show by Items" -
                # exactly what we need to find PO 28792's stock line.
                await page.locator("text=Show by Items").first.click()
                await page.wait_for_timeout(5000)
                await shot(page, "06_pos_to_be_delivered_items")
                print(f"[pos_to_deliver] title={await page.title()}")

                # Search for PO 28792 in this list's search box.
                # User's hunch: maybe this is actually the SAME screen as
                # STO receipts (Inbound Logistics > Inbound Delivery
                # Notifications) - quick check: does searching there for
                # PO 28792 (or its product IDs) turn up anything at all?
                # User's specific hint: Inbound Logistics work center has a
                # "Purchase Orders" view (Inbound Logistics -> Purchase
                # Orders -> select PO -> Post Goods Receipt) - distinct from
                # "Inbound Delivery Notifications" which we already ruled
                # out. Open the Inbound Logistics flyout and list ALL its
                # sub-views first, then click "Purchase Orders" specifically.
                await _wait_for_blocking_layer_clear(page)
                await page.locator('[aria-label="Inbound Logistics"]').first.click(force=True)
                await page.wait_for_timeout(3000)
                await shot(page, "10_inbound_logistics_flyout")
                sub_items = set()
                for el in await page.query_selector_all("a, li, span"):
                    try:
                        if await el.is_visible():
                            t = (await el.inner_text()).strip()
                            if t and len(t) < 60:
                                sub_items.add(t)
                    except Exception:
                        pass
                print(f"[inbound_logistics_flyout] items ({len(sub_items)}):")
                for t in sorted(sub_items):
                    print(f"    - {t}")

                po_link = page.locator("text=Purchase Orders").first
                if await po_link.count() > 0:
                    await po_link.click()
                    await page.wait_for_timeout(4000)
                    await shot(page, "11_inbound_logistics_purchase_orders")
                    print(f"[po_view] title={await page.title()}")
                else:
                    print("[po_view] 'Purchase Orders' link not found in Inbound Logistics flyout")

                # Try the explicit Filter panel (funnel icon) instead of a
                # named saved-query preset - build a raw
                # "Purchase Order ID = 28792" filter against ALL POs,
                # bypassing whatever the saved views are silently scoped by.
                icon_buttons = await page.query_selector_all(".sapMBtnIconLeft, .sapMBtnBase")
                clicked_filter = False
                for b in icon_buttons:
                    try:
                        if await b.is_visible():
                            aria = (await b.get_attribute("aria-label")) or ""
                            title = (await b.get_attribute("title")) or ""
                            if "filter" in (aria + title).lower():
                                await b.click(force=True)
                                clicked_filter = True
                                break
                    except Exception:
                        pass
                print(f"[filter_panel] clicked filter icon: {clicked_filter}")
                await page.wait_for_timeout(2000)
                await shot(page, "17_filter_panel_open")
                if clicked_filter:
                    po_id_input = None
                    for lbl in await page.query_selector_all("label"):
                        try:
                            t = (await lbl.inner_text()).strip()
                            if t == "Purchase Order ID":
                                for_id = await lbl.get_attribute("for")
                                if for_id:
                                    po_id_input = await page.query_selector(f"#{for_id}")
                                break
                        except Exception:
                            pass
                    print(f"[filter_panel] found Purchase Order ID input: {po_id_input is not None}")
                    if po_id_input:
                        await po_id_input.fill("28792")
                        await page.wait_for_timeout(500)
                        go_btn = page.get_by_text("Go", exact=True).first
                        if await go_btn.count() > 0:
                            await go_btn.click(force=True)
                            await page.wait_for_timeout(4000)
                        await shot(page, "18_filter_by_po_id_28792")
                        rows = await page.query_selector_all('tr[id^="__table"]')
                        print(f"[filter_panel] rows after PO ID filter (Open Purchase Orders base): {len(rows)}")

                        # Same PO ID filter, but on top of the broader "All
                        # Purchase Orders by Selection" base view instead of
                        # "Open Purchase Orders" - in case that preset's
                        # hidden status filter is what's excluding 28792.
                        base_dd = page.locator("text=Open Purchase Orders").first
                        if await base_dd.count() > 0:
                            await base_dd.click(force=True)
                            await page.wait_for_timeout(1000)
                            broad_opt = page.get_by_text("All Purchase Orders by Selection", exact=True).first
                            if await broad_opt.count() > 0:
                                await broad_opt.click(force=True)
                                await page.wait_for_timeout(1500)
                                po_id_input2 = await page.query_selector(f"#{await po_id_input.get_attribute('id')}") if await po_id_input.get_attribute('id') else None
                                if not po_id_input2:
                                    for lbl in await page.query_selector_all("label"):
                                        t = (await lbl.inner_text()).strip()
                                        if t == "Purchase Order ID":
                                            for_id = await lbl.get_attribute("for")
                                            if for_id:
                                                po_id_input2 = await page.query_selector(f"#{for_id}")
                                            break
                                if po_id_input2:
                                    await po_id_input2.fill("28792")
                                go_btn2 = page.get_by_text("Go", exact=True).first
                                if await go_btn2.count() > 0:
                                    await go_btn2.click(force=True)
                                    await page.wait_for_timeout(4000)
                                await shot(page, "19_filter_by_po_id_28792_all_pos")
                                rows2 = await page.query_selector_all('tr[id^="__table"]')
                                print(f"[filter_panel] rows after PO ID filter (All POs base): {len(rows2)}")
                                for r in rows2[:5]:
                                    try:
                                        print("   row:", (await r.inner_text()).replace("\n", " | ")[:200])
                                    except Exception:
                                        pass

                # Neither receiving screen shows PO 28792 at all, even with
                # an unrestricted "All Purchase Orders" + exact PO ID
                # filter. Last check: does the PO's own HOME work center
                # ("Purchase Requests and Orders") find it at all, and what
                # does its own Status/Confirmation Control look like?
                await _wait_for_blocking_layer_clear(page)
                await page.locator('[aria-label="Purchase Requests and Orders"]').first.click(force=True)
                await page.wait_for_timeout(3000)
                await shot(page, "20_pro_flyout")
                po_view_link = page.get_by_text("Purchase Orders", exact=True)
                count = await po_view_link.count()
                clicked_pro = False
                for i in range(count):
                    el = po_view_link.nth(i)
                    if await el.is_visible():
                        await el.click(force=True)
                        clicked_pro = True
                        break
                if clicked_pro:
                    await page.wait_for_timeout(4000)
                    await shot(page, "21_pro_purchase_orders_view")
                    print(f"[pro_home] title={await page.title()}")
                    hits = await _search_delivery(page, "28792")
                    print(f"[pro_home] query='28792' -> {hits} row(s)")
                    await shot(page, "22_pro_search_28792")
                    rows3 = await page.query_selector_all('tr[id^="__table"]')
                    for r in rows3[:5]:
                        try:
                            print("   row:", (await r.inner_text()).replace("\n", " | ")[:200])
                        except Exception:
                            pass
                    if rows3:
                        await rows3[0].click(force=True)
                        await page.wait_for_timeout(3000)
                        await shot(page, "23_pro_28792_detail")

                    # Precise filter (not fuzzy free-text) for exact PO ID
                    # 28792 in its own home work center.
                    filter_icon2 = None
                    for b in await page.query_selector_all(".sapMBtnIconLeft, .sapMBtnBase"):
                        try:
                            if await b.is_visible():
                                aria = (await b.get_attribute("aria-label")) or ""
                                title = (await b.get_attribute("title")) or ""
                                if "filter" in (aria + title).lower():
                                    filter_icon2 = b
                                    break
                        except Exception:
                            pass
                    if filter_icon2:
                        await filter_icon2.click(force=True)
                        await page.wait_for_timeout(2000)
                        await shot(page, "24_pro_filter_panel")
                        po_id_input3 = None
                        for lbl in await page.query_selector_all("label"):
                            t = (await lbl.inner_text()).strip()
                            if "Purchase Order ID" in t or t == "Purchase Order":
                                for_id = await lbl.get_attribute("for")
                                if for_id:
                                    po_id_input3 = await page.query_selector(f"#{for_id}")
                                break
                        print(f"[pro_home_filter] found exact PO ID field: {po_id_input3 is not None}")
                        if po_id_input3:
                            await po_id_input3.fill("28792")
                            go_btn3 = page.get_by_text("Go", exact=True).first
                            if await go_btn3.count() > 0:
                                await go_btn3.click(force=True)
                                await page.wait_for_timeout(4000)
                            await shot(page, "25_pro_exact_28792")
                            rows4 = await page.query_selector_all('tr[id^="__table"]')
                            print(f"[pro_home_filter] exact rows: {len(rows4)}")
                            for r in rows4[:5]:
                                try:
                                    print("   row:", (await r.inner_text()).replace("\n", " | ")[:250])
                                except Exception:
                                    pass
                else:
                    print("[pro_home] 'Purchase Orders' link not found under Purchase Requests and Orders")

            finally:
                await browser.close()
                playwright_concurrency.unregister_browser(browser)
    finally:
        playwright_concurrency.release()

    print("DONE")


if __name__ == "__main__":
    asyncio.run(main())
