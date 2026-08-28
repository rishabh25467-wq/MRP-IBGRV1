"""Caps concurrent headless Playwright/SAP-UI sessions at 3 across the
whole backend (Aug 2026, user's explicit ask) - so N warehouses
receiving/shipping at once can never spin up more than 3 headless
Chromium instances in this container at a time; anything beyond that
queues instead of racing for CPU/memory and risking container OOM.

A plain threading.Semaphore (NOT asyncio.Semaphore) on purpose: the two
call sites (sap_playwright_pgr_service.py, sap_playwright_outbound_gi_
service.py) each acquire/release it from inside their own async
function, but those functions get invoked from different execution
contexts - one straight from the main event loop, the other via
asyncio.run() inside a asyncio.to_thread() worker thread (its own
private event loop). An asyncio.Semaphore's wakeup is Future-based and
isn't safe to release() from a different thread/event-loop than the
one that awaited it; the blocking wait always happens on a plain
worker thread instead, never on an event loop itself.

`acquire()`/`release()` below deliberately park that blocking wait on
their OWN dedicated executor (`_QUEUE_WAIT_EXECUTOR`), not the app-wide
asyncio default executor (see testing_agent iteration_133 code review)
- server.py's every other `asyncio.to_thread()` call (DB reads, every
SAP client, etc.) shares that one default pool, so a user selecting
many pending STOs at once (each queuing a job that blocks a thread
until its semaphore turn) could otherwise exhaust it and stall
unrelated requests app-wide."""
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

MAX_CONCURRENT_SAP_UI_SESSIONS = 3
sap_ui_semaphore = threading.Semaphore(MAX_CONCURRENT_SAP_UI_SESSIONS)

_QUEUE_WAIT_EXECUTOR = ThreadPoolExecutor(max_workers=64, thread_name_prefix="sap-ui-queue-wait")

# Every headless browser launched while holding the semaphore registers
# itself here so a backend shutdown can close them explicitly instead of
# leaving orphaned Chromium processes behind (testing_agent iteration_133:
# 18 stray chrome processes survived a hot-reload with 3 jobs in flight).
_active_browsers = set()


async def acquire() -> None:
    await asyncio.get_running_loop().run_in_executor(_QUEUE_WAIT_EXECUTOR, sap_ui_semaphore.acquire)


def release() -> None:
    sap_ui_semaphore.release()


def register_browser(browser) -> None:
    _active_browsers.add(browser)


def unregister_browser(browser) -> None:
    _active_browsers.discard(browser)


async def close_all_browsers() -> None:
    for browser in list(_active_browsers):
        try:
            await browser.close()
        except Exception:
            pass
        _active_browsers.discard(browser)
