# PRD - SAP BOM Viewer / Production Planning App

> Detailed, dated implementation history lives in `/app/memory/CHANGELOG.md` (append new entries at
> the top). This file is the static problem statement + architecture + current backlog only.

## Original problem statement
Extend a SAP BOM viewer application into a full production-planning suite for Radish Technologies:
- Production Plan page integrating an external OMS Open-PO Demand feed; 2-Tier MRP system.
- Microsoft Entra ID (Azure AD) SSO for internal staff.
- Supplier Portal (external vendors) with its own JWT auth (Emergent Auth).
- Purchase Order Creation automation.
- App-side Inbound STO Receipt page that groups multiple SAP delivery lines into one clickable row.
- Headless Playwright automation to perform SAP Goods Receipts (PGR) where native SAP APIs block it.

## Architecture
- `/app/backend/`: FastAPI + MongoDB. SAP ByDesign integration via SOAP and OData clients
  (`sap_sto_client.py`, `sap_gsa_write_client.py`, `sap_inbound_delivery_client.py`,
  `sap_goods_movement_client.py`, `sap_outbound_delivery_client.py`,
  `sap_playwright_pgr_service.py`), plus Microsoft Entra ID SSO + custom JWT (Supplier Portal) auth.
- `/app/frontend/src/`: React SPA, Shadcn UI components.
- Key 3rd-party integrations: SAP Business ByDesign (SOAP + OData +, as of this session, real UI
  automation via Playwright for Goods Receipt), Microsoft Entra ID SSO, Emergent Auth (JWT),
  OpenAI GPT-5.4-mini (Emergent LLM Key) for AI Quick Entry, Emergent Object Storage.

## Core features (built)
- BOM Explorer, Production Plan / 2-Tier MRP, Purchasing Plan.
- Stock Transfer Orders (STO): create, release, Goods Issue (per-line and combined-delivery attempts),
  Inbound STO Receipt (groups SAP delivery lines, posts Goods Receipt via Playwright - see CHANGELOG).
- Supplier Portal: PO acceptance, shipment creation, GRN admin approval + SAP GSA posting.
- Purchase Order Creation automation.
- Internal GRN admin approval + posting (Goods Movement).
- Delivery Challan / Gate Pass print pages (`DeliveryNotePage.js`, `GatePassPage.js`).

## Session Aug 31 2026 - fixes
1. **Create-and-release retry loop (proposal 226316)**: `_continue_order_creation`
   (server.py) now takes `is_resume` - on Resume/Retry only, it first scans for an
   already-existing "In Preparation" order matching material+qty (new helper
   `_find_existing_prep_order_for_material`) and releases it directly, instead of
   capturing a fresh baseline that always excluded the pre-existing order and looped
   uselessly re-triggering "Request Production" for 20 min every retry.
2. **Outbound GI "Release button disabled" false positive** (Order 30518/Delivery
   P1D1-492): `sap_playwright_outbound_gi_service.py`'s Release-button check now
   retries up to 4x (clicking "Check Consistency" each time, 8s+6s waits) before
   concluding a real SAP-side rejection - SAP recomputes Consistency Status
   ASYNCHRONOUSLY after the metadata Save, and the old 5s wait was systematically
   too short on this tenant, so every Retry hit the identical false-positive.
3. **"SAP User" column missing from STO list table**: `gi_playwright_user` was
   already returned by the API and shown in the single-order detail view, but never
   rendered as a column in the "Recent Stock Transfer Orders" table - added.
   Historical orders (before this field existed) and all single-line STOs (no
   Playwright/SAP-UI login involved, pure API path) correctly show "—".
4. **Delivery Challan print format** (user-annotated PDF): removed Chrome's own
   default print header/footer (timestamp, "Materials Hub" page title, page URL -
   never rendered by app code, pure browser print-dialog default) via
   `@page { margin: 0 }` in a print-only `<style>` tag, with the document's own
   padding restored (`print:p-10`) to keep real margins. Also changed "Transport"
   label to "Transport Details" (renders as "TRANSPORT DETAILS"). Same fix applied
   to `GatePassPage.js`. Verified end-to-end with a real generated PDF via Playwright.
5. **"Waiting forever" once a multiline Delivery already exists** (Order 30518, 2nd
   incident): once an earlier attempt created a real combined Outbound Delivery,
   its request items permanently vanish from SAP's "pending" list -
   `try_post_goods_issue` used to only ever check that list and report "waiting"
   forever. New `_try_release_existing_multiline_delivery` (stock_transfer_service.py)
   checks for an already-known delivery FIRST and releases it via a fast direct API
   call (new `get_delivery_object_id_by_id` on sap_outbound_delivery_client.py),
   falling back to the Playwright UI (new `release_existing_delivery_via_ui`) only
   if the API attempt fails.
6. **Unbounded hang, no per-attempt ceiling** (Order 30529 - stuck with NOTHING
   created yet in SAP, confirmed live via API + user's own SAP screenshot): a single
   Playwright attempt had no timeout, so the 20-min job-level ceiling could never
   fire (blocked waiting on that one hung thread). Added `GI_PLAYWRIGHT_ATTEMPT_TIMEOUT_SECONDS`
   (300s) wrapping every attempt in `asyncio.wait_for`.
7. **UI staleness**: the order detail popup was a frozen snapshot (never refreshed
   while open) - now syncs with the list on every list refresh; list itself now
   auto-refreshes every 20s while any order has `gi_job_running`.
8. **Debug screenshot per step + Force Stop button** (user's explicit ask, after
   4 straight incidents): `_save_debug_screenshot` now takes a `step` label, called
   at every phase transition (not just failures), timestamped, capped at 20/order
   (`MAX_DEBUG_SCREENSHOTS_PER_ORDER`). New endpoints
   `GET /stock-transfer/orders/{sto_id}/debug-screenshots` +
   `GET /stock-transfer/debug-screenshots/{filename}`, viewer in `OrderDetailBody`
   (admin-only). New `POST /stock-transfer/orders/{sto_id}/force-stop-gi` (backed by
   `request_gi_job_stop`/`is_gi_stop_requested`) - instant UI feedback (flips to
   "failed" immediately), background loop exits quietly once it notices the flag
   (bounded by the same 5-min ceiling). Button only shows when `gi_job_running` is
   true or `gi_status === "insufficient_stock"` (mirrors backend validation exactly
   - first version showed it too broadly and produced a false-positive error toast).
- Preview env note: `/pw-browsers` Chromium install disappeared twice this session
  (pod restart wipes ephemeral storage) - re-ran `playwright install chromium`
  each time; not a code issue.
- Build version footer already exists (`/api/version`, `Footer.jsx`) - "Build
  {commit} · {date}" on every page, useful for the user to verify a redeploy
  actually picked up the latest fixes.
  - **Root cause of blank footer in production found (Sep 2026)**: production
    deploys have no `.git` history, so the old `git log`-at-runtime approach
    always failed silently there. Fixed: `_get_build_version()` (server.py) now
    reads a committed `backend/build_info.json` first, falling back to live git
    only for local/preview dev. **New script `backend/generate_build_info.py`
    must be run (regenerating + committing build_info.json) right before every
    publish/deploy** - it captures the commit hash at generation time, so it's
    only accurate as of whenever it was last run. Also fixed: build-version
    footer was missing entirely on Login/Pending-Access screens and the whole
    Supplier Portal route tree (separate layout, never had `<Footer/>`) -
    added to both in App.js.
- Told user directly (their explicit challenge on reliability): the multi-line
  Goods Issue automation cannot be promised 100% consistent - it drives SAP's live
  Fiori UI screen-by-screen because no API exists to combine multi-line deliveries
  (8 dead-end API attempts already documented). Single-line orders use a pure API
  path and don't share this risk class at all.

## Session Sep 1 2026 - Native SAP Custom BO (ABSL) to replace multi-line Outbound Delivery Playwright automation
Built entirely in a SEPARATE TEST TENANT (`my441464.businessbydesign.cloud.sap`, NOT the production
tenant used elsewhere) via SAP Cloud Applications Studio - a proof-of-concept to eventually replace the
flaky `sap_playwright_outbound_gi_service.py` UI automation with a native SAP backend action.
- Solution "Combinedoutbounddelivery", custom BO `BusinessObject1` (elements: OrderReferenceID
  [AlternativeKey, type ID], DeliveryID, VehicleNo, TransportMode, PlaceOfSupply, GRNo, DateOfSupply,
  Status, DeliveryRequestUUID) with two actions:
  - `Combine`: loops `outboundDeliveryRequest.Item.ItemScheduleLine` and calls the SAP-standard
    `RequestDeliveryExecution(TaskBasedIndicator, AllowSplitIndicator, TargetSiteLogisticsRequestUUID,
    SplitByShippingOrPickupDateTimeIndicator, SplitByOrderIndicator, SplitByDeliveryPriorityCodeIndicator)`
    on each, all split indicators false, forcing ONE combined Outbound Delivery.
  - `SetTransportDetailsAndRelease`: queries `OutboundDelivery` by `DeliveryID`, calls `.Release()`
    (which also posts Goods Issue). GR/vehicle/transport `_KUT` custom field writes were EXPLICITLY
    DROPPED per user's request mid-session - this action now only releases/posts.
  - Both actions are "mass-enabled" (the auto-generated script's `this` is a COLLECTION of Root
    instances, not a single instance - must `foreach (var item in this)`, a real gotcha that cost
    several iterations before being caught via GitHub Copilot's read of the file's own header comment).
- Exposed as TWO separate SOAP web services (WebService1=Combine, WebService2=SetTransportDetailsAndRelease),
  both CRUD+Query+Action, under a shared Work Center View `CombineView`.
- **BLOCKED at session end**: `Admin`/`Admin@11336` business user has NO business role assigned
  ("No Active Business Roles available"), and `CombineView` could not be found via "Find Business Role
  by Work Center View" search NOR in the "Work Center and View Assignment" tab's catalog - the custom
  PDI-created view doesn't appear to be registered in the runtime Business Role/authorization catalog
  yet. SOAP calls fail with `Authorization role missing for service ... operation Create` (HTTP 500
  SOAP fault) - this is a genuine SAP Business Configuration/authorization gap, not a code bug. User
  called HOLD on this - needs SAP Basis/admin expertise (likely: the custom Work Center itself, not
  just the View, may need explicit Business Configuration scoping/activation, or a different broader
  admin business role needs to be identified and assigned instead).
- Confirmed WORKING (live-tested against test tenant): the SOAP request/response XML structure itself -
  root wrapper elements (`BusinessObject1CreateRequest_sync` etc.) live in namespace
  `http://sap.com/xi/SAPGlobal20/Global`, inner fields (`BasicMessageHeader`, `BusinessObject1`,
  `Selection*`) are UNQUALIFIED (no `elementFormDefault` set anywhere in the WSDL). `QueryByElements`'s
  real message is `BusinessObject1QueryByElementsSimpleByRequest_sync` with a
  `BusinessObject1SimpleSelectionBy` wrapper (not a generic name). Endpoints:
  `https://my441464.businessbydesign.cloud.sap/sap/bc/srt/scs/sap/yy0cq5p7hy_webservice{1,2}?sap-vhost=...`.
  Standalone test script: `/app/backend/test_custom_bo_soap.py` (NOT wired into main app).
- Local SAP Cloud Studio tooling bugs hit repeatedly this session (documented for future reference,
  none are code/logic errors): (1) pasting text with "." into the ABSL editor can silently drop every
  dot character (worked around via paste-with-"@"-placeholder then Find&Replace "@"->"."); (2) the
  Web Service wizard's "Add > Create New View" step reliably fails with
  `WsAuthWOC_View.tt: System.NotSupportedException: The invoked member is not supported in a dynamic
  assembly` (a broken CopernicusIsolatedShell/T4 templating issue) - worked around by creating the Work
  Center View as a standalone Solution Explorer item (Add New Item > Work Center View) instead, which
  uses a different, working code path; (3) the wizard only lets you define ONE action-operation per
  run despite selecting multiple actions in the checklist - needed two separate web services.
- See `/app/memory/test_credentials.md` for full endpoint/credential details.

## Known SAP-side structural limitations (do not re-investigate, already conclusively proven)
- `PGRBackground` (Post Goods Receipt) via OData is disabled for this tenant's Inbound Delivery
  Notifications - official SAP KBA 3583076 confirms Actual Quantity can't be passed this way. Fixed
  via Playwright UI automation (`sap_playwright_pgr_service.py`).
- Site Logistics Task API doesn't apply - this tenant has 0 task-based inbound execution tasks.
- GSA (Goods and Service Acknowledgement) only works for non-stock/service PO lines, never real stock
  materials.
- No Inbound Delivery Notification is created at all for PO-sourced (external supplier) stock items in
  this tenant - only Stock-Transfer-sourced ones exist. Root cause of why Supplier Portal GRN used GSA
  as a (broken, for stock items) workaround.
- A multi-line STO producing ONE combined Outbound Delivery (instead of one per line) is NOT possible
  via any SAP API - 8 documented dead-end attempts (PGIInBackground, SLRequestDeliveryExecution,
  OutboundDeliveryRequestAllocate, SLPGIInBackground, PartialDeliveryControlCode="3") - this tenant's
  Ship-to Party Account Master Data silently forces per-line deliveries regardless of payload. Fixed
  via a second Playwright UI automation (`sap_playwright_outbound_gi_service.py`, Aug 28 2026) - see
  CHANGELOG. Vehicle No./Transportation Mode/Place Of Supply/G.R No./Date Of Supply/G.R Date are real
  SAP extension fields (`_KUT` suffix) but reject direct API writes once SAP's scheduler picks up the
  document - only writable via the SAP UI, which this same Playwright script now also does for
  multi-line orders. "Freight Forwarder" needs a real SAP Business Partner lookup (breaks Consistency
  Status as free text) - stays Note-only, deliberately not attempted via UI.

## Current backlog

### P0
- SAP Custom BO SOAP authorization (Sep 1 2026 session) - `Admin` test-tenant user's Create call fails
  with "Authorization role missing"; `CombineView` work center view isn't discoverable in the Business
  Role/Work Center assignment catalog. BLOCKED, needs SAP Basis/admin to resolve (see CHANGELOG-style
  session note above) before the standalone SOAP test script can even validate Create/Read, let alone
  Combine/Release. This whole effort is still in a TEST tenant only - production rollout is a distinct
  future step after this is proven.
- Supplier Portal GRN automation - BUILT & UNIT/INTEGRATION TESTED this session (2026-08-29, see
  CHANGELOG), but the "Post Goods Receipt" dialog's field-filling has NEVER run against a real,
  existing PO (every test used a deliberately fake PO number for safety). Needs ONE supervised live
  test against a real, low-risk PO before this is trusted for unattended use - ask the user to
  nominate one.
- Possible data-integrity gap flagged (not yet investigated further, user said to leave it for now):
  Supplier Portal PO cache may be showing external vendors POs that are still "In Preparation"
  (unreleased draft) in SAP as if they were open orders to fulfil - PO 28792 was found in this state.
- Phase 5: External QMS feed - secured API/data feed (API key) for external QMS app to pull
  pending-QC items.

### P1
- Preview pod's `/pw-browsers` Chromium install can silently disappear on a pod
  restart (observed twice, Aug 31 2026 session) - re-run `playwright install chromium`
  with `PLAYWRIGHT_BROWSERS_PATH=/pw-browsers` if any Playwright job errors with
  "Executable doesn't exist". Not a code bug, just preview-env ephemeral storage.
- Chromium under Emergent's standard 1Gi/250m pod tier risks OOM under concurrent Playwright load
  (deployment-scan WARN, 2026-08-29) - the eager startup warm-up that made this WORSE (crashed
  production immediately on boot) was removed same day; the underlying per-job resource cost during
  real concurrent SAP UI automation is still a pod-sizing decision, not fixed by code.
- MSSQL ERP integration (`erp_portal_client.py`) needs confirmed outbound K8s egress to its on-prem
  IPs in production (deployment-scan WARN, 2026-08-29).
- A few unbounded `find({})` calls need pagination (deployment-scan WARN, 2026-08-29):
  `store_approval_service.list_all_requests`, `mrp_plan_store.list_named_plans`,
  `supplier_shipment_service.list_shipments`, `server.py admin_list_users`.
- `sap_valuation_client`'s blocking sync work shares the default thread pool and can starve ALL
  `/api/*` requests for 60s+ intermittently (flagged by testing_agent, iteration_138/139) - needs a
  dedicated executor or an async HTTP client + lower per-call timeouts. Not yet fixed.
- Multiple SAP business users for Playwright concurrency (2-3, possibly split by warehouse) - would
  fix a real risk where concurrent jobs sharing one SAP login can force-kick each other via "Delete
  all sessions?" on login. User said "I'll decide, revisit later" (2026-08-28) - needs 2-3 new SAP
  UI-capable business user credentials from the user before implementing.
- e-Way Bill and e-Invoice integration via Sandbox.co.in - blocked on user providing API credentials.
- Per-Line Ship Status - STO detail screen should show each line's own shipped/pending status, not one
  combined status.
- Auto-Refresh After Transfer - refresh a site's stock right after its own STO posts.
- Import Preview - before/after diff table before committing a bulk Excel import.
- Missing Weight Alert - flag components with no weight data in the source report.
- Auto-Retry ERP Sync - automatically retry a failed ERP sync a few times in the background.
- Goods Movement "RESTRICTED Quality Inspection" stock status - rejected by SAP tenant currently;
  GRN step 2 does a plain move only (see test_credentials.md for the live-tested finding).
- Non-blocking background "Receive" dialog + multi-select bulk STO Inbound Receipt posting (frontend
  UX only - lets user close the Inbound Receipt modal while the Playwright PGR job runs, and select
  several STOs to receive at once). Independent of the Outbound combine fix (Aug 28 2026).
- Observability gaps flagged by testing agent (iteration 132, not bugs, no regression found): (1)
  single-line STOs still don't get the 5 SAP `_KUT` metadata fields (only multi-line/Playwright path
  writes them) - decide if single-line should also route through Playwright just for metadata; (2) a
  silently-failed metadata fill inside `_fill_delivery_metadata` doesn't get flagged on the STO doc.

### P2
- Fix stuck historical SAP orders (30280/30336/30385) - blocked on user + SAP consultant.
- Delivery Note Batch Print, Fuzzy Product Match (AI Quick Entry), Bulk Finish (reporting points),
  Category Picker (Create Material dropdown from SAP's Product Category list).

### P3 (future)
- Backfill old CompCode on already-synced legacy ERP rows.
- Incorporate Quality/OTIF into AI Quota Suggestions.
