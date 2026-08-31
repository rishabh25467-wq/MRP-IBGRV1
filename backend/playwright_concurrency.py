"""Dedicates each of up to 3 concurrent headless Playwright/SAP-UI
sessions its OWN SAP login from a fixed pool of 3 (Aug 31 2026, user's
explicit ask, following a real "Playwright SAP Login Collision" P1: SAP
kicks out an existing session the instant the SAME user logs in again
from a second browser, so 2 concurrent jobs sharing one login could boot
each other out mid-task) - so N warehouses receiving/shipping at once
can never spin up more than 3 headless Chromium instances in this
container at a time, AND never share a SAP login while doing so;
anything beyond 3 queues for a free (browser slot, credential) pair
instead of racing for CPU/memory/SAP sessions.

A plain queue.Queue (NOT asyncio.Queue) on purpose: the three call sites
(sap_playwright_pgr_service.py, sap_playwright_outbound_gi_service.py,
sap_playwright_supplier_pgr_service.py) each acquire/release it from
inside their own async function, but those functions get invoked from
different execution contexts - one straight from the main event loop,
another via asyncio.run() inside a asyncio.to_thread() worker thread
(its own private event loop). An asyncio-native primitive's wakeup is
Future-based and isn't safe to interact with from a different
thread/event-loop than the one that awaited it; the blocking wait
always happens on a plain worker thread instead, never on an event
loop itself.

`acquire()`/`release()` below deliberately park that blocking wait on
their OWN dedicated executor (`_QUEUE_WAIT_EXECUTOR`), not the app-wide
asyncio default executor (see testing_agent iteration_133 code review)
- server.py's every other `asyncio.to_thread()` call (DB reads, every
SAP client, etc.) shares that one default pool, so a user selecting
many pending STOs at once (each queuing a job that blocks a thread
until its turn) could otherwise exhaust it and stall unrelated requests
app-wide."""
import asyncio
import logging
import os
import queue
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

SAP_UI_CREDENTIAL_POOL = [
    (os.environ["SAP_USERNAME"], os.environ["SAP_PASSWORD"]),
    (os.environ["SAP_UI_USERNAME_2"], os.environ["SAP_UI_PASSWORD_2"]),
    (os.environ["SAP_UI_USERNAME_3"], os.environ["SAP_UI_PASSWORD_3"]),
]
MAX_CONCURRENT_SAP_UI_SESSIONS = len(SAP_UI_CREDENTIAL_POOL)

_credential_pool = queue.Queue()
for _credential in SAP_UI_CREDENTIAL_POOL:
    _credential_pool.put(_credential)

_QUEUE_WAIT_EXECUTOR = ThreadPoolExecutor(max_workers=64, thread_name_prefix="sap-ui-queue-wait")

# Plain counters (not derivable from a queue.Queue itself) backing
# the frontend's global concurrency badge (user's explicit ask, Aug 2026) -
# approximate-but-good-enough for a status display, not used for any
# actual gating logic.
_status_lock = threading.Lock()
_active_count = 0
_queued_count = 0

# Every headless browser launched while holding a credential registers
# itself here so a backend shutdown can close them explicitly instead of
# leaving orphaned Chromium processes behind (testing_agent iteration_133:
# 18 stray chrome processes survived a hot-reload with 3 jobs in flight).
_active_browsers = set()

_chromium_verified = False
_chromium_verify_lock = threading.Lock()


async def acquire() -> tuple:
    """Blocks until a (browser slot, dedicated SAP login) pair is free -
    returns that login as a (username, password) tuple. MUST be paired
    with `release(credential)` using the SAME tuple, in a finally block."""
    global _queued_count, _active_count
    with _status_lock:
        _queued_count += 1
    try:
        credential = await asyncio.get_running_loop().run_in_executor(_QUEUE_WAIT_EXECUTOR, _credential_pool.get)
    except Exception:
        with _status_lock:
            _queued_count -= 1
        raise
    with _status_lock:
        _queued_count -= 1
        _active_count += 1
    return credential


def release(credential: tuple) -> None:
    global _active_count
    _credential_pool.put(credential)
    with _status_lock:
        _active_count = max(0, _active_count - 1)


def get_concurrency_status() -> dict:
    with _status_lock:
        return {"active": _active_count, "queued": _queued_count, "max": MAX_CONCURRENT_SAP_UI_SESSIONS}


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


def _verify_chromium_sync(original_error: Exception) -> None:
    """Runs the actual blocking lock-acquire + `playwright install chromium`
    subprocess. MUST always be called via `_QUEUE_WAIT_EXECUTOR` (never
    straight off the event loop) - `_chromium_verify_lock` is a plain
    `threading.Lock`, and a second concurrent caller blocked on
    `.acquire()` while still on the asyncio event loop thread would
    freeze the ENTIRE server for up to the full 180s timeout below (real
    incident, Aug 29 2026 - fixed by moving this off the event loop)."""
    global _chromium_verified
    with _chromium_verify_lock:
        if _chromium_verified:
            return
        logger.warning(f"Chromium launch failed ({original_error}) - attempting a one-time self-heal via `playwright install chromium`")
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True, text=True, timeout=180,
        )
        if result.returncode != 0:
            raise RuntimeError(f"playwright install chromium failed: {result.stderr[-500:]}") from original_error
        _chromium_verified = True


async def launch_chromium(playwright):
    """Use instead of `playwright.chromium.launch(headless=True)` directly.

    Chromium is EXPECTED to already be baked into the deployment image at
    build time (PLAYWRIGHT_BROWSERS_PATH=/pw-browsers, matching the
    pinned `playwright==1.62.0`), per Emergent Support's guidance - a
    proactive/eager self-heal was removed Aug 29 2026 because it competed
    for CPU during the platform's boot health-check window.

    Re-added Aug 31 2026 as a LAZY, on-demand-only safety net after a
    real production outage: despite that guidance, a live deploy still
    had NO Chromium binary at runtime (confirmed: raw "Executable doesn't
    exist" errors leaking straight to users on the Inbound Receipt
    screen, with nothing left to catch it once the previous self-heal was
    removed). Unlike the removed eager version, this only triggers the
    FIRST time an actual job needs Chromium (long after boot/health-check
    has already passed, and via `_QUEUE_WAIT_EXECUTOR` off the event
    loop - see `_verify_chromium_sync`), so it can't cause the earlier
    boot-time CPU contention. `_chromium_verified` is a process-wide
    latch so a genuinely broken install (no disk/network) fails fast on
    every launch after the first attempt instead of eating a ~180s
    reinstall timeout every single time."""
    try:
        return await playwright.chromium.launch(headless=True)
    except Exception as e:
        if _chromium_verified or "Executable doesn't exist" not in str(e):
            raise
        await asyncio.get_running_loop().run_in_executor(_QUEUE_WAIT_EXECUTOR, _verify_chromium_sync, e)
        return await playwright.chromium.launch(headless=True)
