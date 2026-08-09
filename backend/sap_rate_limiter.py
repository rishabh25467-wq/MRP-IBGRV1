"""Global concurrency limiter shared by every SAP client (SOAP + OData).

SAP Business ByDesign tenants enforce a small concurrent web-service
session limit per tenant, SEPARATE from the browser UI's own session pool
(a human working in the SAP UI directly is never affected by this limit).
This backend runs several always-on background jobs (BOM cache refresh
every 6h, inventory cache refresh every 2h, drawing-URL backfill every
15min - each spinning up their own ThreadPoolExecutor with several
workers) on top of live user-triggered requests (BOM search, Purchasing
Plan generation, Cost Estimate Run, etc). Without a shared cap across all
of these, they can collectively exceed SAP's concurrent-session limit and
start getting connection timeouts/500s that look like a "SAP outage" but
are actually self-inflicted - confirmed by the fact a human browsing the
same tenant in parallel sees no such issue at all.

Every direct `requests.get/post`/`session.get/patch` call to the SAP host
anywhere in this codebase should be wrapped in `with sap_semaphore:` so the
total in-flight request count across ALL background jobs + live requests
never exceeds this cap, regardless of which ThreadPoolExecutor or
asyncio.to_thread call spawned it - a plain threading.Semaphore is shared
correctly across every thread in this single-process backend."""
import threading

SAP_MAX_CONCURRENT_REQUESTS = 3

sap_semaphore = threading.Semaphore(SAP_MAX_CONCURRENT_REQUESTS)
