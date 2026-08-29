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
import logging
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

MAX_CONCURRENT_SAP_UI_SESSIONS = 3
sap_ui_semaphore = threading.Semaphore(MAX_CONCURRENT_SAP_UI_SESSIONS)

_QUEUE_WAIT_EXECUTOR = ThreadPoolExecutor(max_workers=64, thread_name_prefix="sap-ui-queue-wait")

# Plain counters (not derivable from a threading.Semaphore itself) backing
# the frontend's global concurrency badge (user's explicit ask, Aug 2026) -
# approximate-but-good-enough for a status display, not used for any
# actual gating logic.
_status_lock = threading.Lock()
_active_count = 0
_queued_count = 0

# Every headless browser launched while holding the semaphore registers
# itself here so a backend shutdown can close them explicitly instead of
# leaving orphaned Chromium processes behind (testing_agent iteration_133:
# 18 stray chrome processes survived a hot-reload with 3 jobs in flight).
_active_browsers = set()

_chromium_verified = False
_chromium_verify_lock = threading.Lock()


async def acquire() -> None:
    global _queued_count, _active_count
    with _status_lock:
        _queued_count += 1
    try:
        await asyncio.get_running_loop().run_in_executor(_QUEUE_WAIT_EXECUTOR, sap_ui_semaphore.acquire)
    except Exception:
        with _status_lock:
            _queued_count -= 1
        raise
    with _status_lock:
        _queued_count -= 1
        _active_count += 1


def release() -> None:
    global _active_count
    sap_ui_semaphore.release()
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
    freeze the ENTIRE server (every unrelated request/health-check) for
    up to the full 180s timeout below, not just its own job (real
    production incident, Aug 29 2026: two Goods Receipt jobs landed on a
    freshly-deployed pod at the same moment, before Chromium had ever
    been verified, and the second job's blocking lock-acquire on the
    main event loop thread froze the whole app long enough for
    Cloudflare to return a 520)."""
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
    """Use instead of `playwright.chromium.launch(headless=True)` directly
    - self-heals ONCE if the on-disk browser cache is missing/mismatched
    against the pinned `playwright` pip version (real incident, Aug 28
    2026: a forked pod's /pw-browsers still had an older Chromium
    revision than playwright==1.62.0 expects, so every Goods Issue
    attempt failed instantly and silently retried for the full 20-min
    poll window before anyone noticed - `pip install playwright` only
    installs the Python wrapper, never the browser binaries themselves).
    `_chromium_verified` is a process-wide latch so a genuinely broken
    install (e.g. no disk space, no network) fails fast on every launch
    after the first attempt instead of eating a ~180s reinstall timeout
    every single time.

    NOTE (Aug 29 2026): deliberately does NOT get proactively "warmed up"
    at server startup anymore - a boot-time eager launch used to add a
    real ~200MB Chromium memory spike at the exact moment the process is
    also opening its Mongo/MSSQL/SAP connections, which is suspected to
    have OOM-crashed the container on Emergent's standard production pod
    tier (measured live: one idle Chromium instance = ~194MB RSS, on top
    of everything else starting up at once). The lazy self-heal below
    already can't freeze the event loop (see `_verify_chromium_sync`),
    so paying that cost lazily on the first real job - spread out in
    time instead of concentrated at boot - is strictly safer."""
    try:
        return await playwright.chromium.launch(headless=True)
    except Exception as e:
        if _chromium_verified or "Executable doesn't exist" not in str(e):
            raise
        await asyncio.get_running_loop().run_in_executor(_QUEUE_WAIT_EXECUTOR, _verify_chromium_sync, e)
        return await playwright.chromium.launch(headless=True)
