## BUG FIX: "user" role couldn't see their OWN Recent Activity / self-created orders (2026-08-28)

- **Root cause**: `get_proposal_and_release_history` and `get_open_production_lots`'s "self-created
  only" filters matched purely on a free-text display name string (`actor`/`released_by`/`created_by`
  vs the logged-in user's current Azure AD `name` claim) - any drift between the two (real incident:
  Mayank Jadon's own Proposal 226253 invisible on his own screen, visible to admin) silently hides a
  user's own data with no error.
- **Fix**: every new proposal/order now also stores the creator's STABLE Entra ID identity
  (`tid:oid`, `request.state.user["_id"]`) as `actor_user_id`/`released_by_user_id`/
  `created_by_user_id` alongside the display name. Filtering now matches on that stable id first
  (immune to name-string drift), falling back to the old name match only for entries logged before
  this fix. Applied in `production_confirmation_service.py` (`log_proposal_creation`,
  `log_order_release`, `get_order_creators`) and `server.py` (`create_production_proposal`,
  `create_and_release_production_order`, `release_production_order`,
  `resume_failed_create_and_release_job`, `get_open_production_lots`, `get_proposal_and_release_history`).
  No frontend changes needed - identity is derived server-side from the session, never trusts the client.
- Verified live via curl: seeded a proposal with `actor_user_id` set, then drifted the account's `name`
  (added an internal double-space) - entry still correctly matched as "mine". A legacy entry with no
  `actor_user_id` still matches via the old name fallback. Does NOT retroactively fix very old entries
  that predate this change and were already affected by a name mismatch.


## Clarification (2026-08-27) - stale "Active Orders" rows on Create Production Order page

- Explained to user: Recent Activity only logs once a Proposal is actually created in SAP; rows stuck
  at "Checking Stock" have nothing to log yet. Reproduced the specific stuck-row complaint: the 2
  visible rows were stale `localStorage` job references with no matching backend job doc (from before
  an environment reset) - confirmed the existing 404-detection auto-clear (Aug 2026) already handles
  this correctly within a few seconds of an active poll; likely just browser tab timer throttling made
  it look permanently stuck. No code change needed, self-heals on refresh.




- **Site + Warehouse selection on GRN Approval**: `GET /admin/grn/sites` returns the internal staff
  member's own `bound_sites` (auto-locks the Site dropdown when there's exactly one - reuses the
  existing generic "Store Assignment" binding on Access Management, no new admin UI needed) or every
  known site for admin/super_admin. `GET /admin/grn/warehouses/{site_id}` (403 if not bound to that
  site) loads real SAP warehouses for the chosen site via `stock_transfer_service.list_known_warehouses_for_site`.
  One warehouse applies to the WHOLE shipment (user's explicit ask - never per-line).
- **Supplier Invoice Number** (free-text `supplier_doc_num`) captured at approval time.
- **Discrepancy flow**: RECEIVING STORE staff (not the vendor) call `POST /admin/grn/{code}/discrepancy`
  with a free-text reason + specific mismatched line item(s) - status becomes `discrepancy`, no SAP
  write happens. The vendor can still edit their OWN shipment while `discrepancy` (same as `in_transit`)
  - any such edit auto-clears the discrepancy and resets status back to `in_transit` for re-review.
  Staff can also Approve/Reject directly from `discrepancy` as an override.
- **Live 2-step SAP write on Approve**: step 1 (unchanged) posts the real Goods Receipt (GSA) per
  distinct PO in the shipment; step 2 (NEW), only attempted if step 1's `sap_sync_status` becomes
  `posted`, calls `sap_goods_movement_client.goods_movement()` to move each line's qty into the chosen
  warehouse - tracked separately as `sap_movement_status` (`not_applicable`/`pending`/`posted`) with a
  `POST /admin/grn/{code}/retry-movement` retry action in the UI.
  - **Live-tested this session** (user's explicit go-ahead): approved a dummy shipment (2 EA of
    P27175) - GSA POST genuinely reached SAP and got a real SAP-side rejection (expected, PO doesn't
    exist in SAP - proves connectivity/schema, not a bug).
  - **CONFIRMED TENANT LIMITATION** (live-tested via a real, immediately-reversed 1 EA P2-RM->P2-QC
    movement, then isolated each field): setting `InventoryStockStatusCode`/`InventoryRestrictedUseIndicator`
    on the Goods Movement target - EITHER alone - is rejected ("No inventory items found for external
    id...") by this SAP tenant; a plain move succeeds. GRN's step 2 today does a PLAIN move only - true
    "RESTRICTED Quality Inspection" stock needs either the product's own SAP Inspection Plan config (so
    the GSA's own automatic receiving flow routes it there) or a different SAP service - open item, not
    solved this session.
- **Perf/consistency fixes from testing_agent (iteration 125, 100% functional pass, no defects)**:
  `inventory_service.list_known_sites()` now has a 5-min in-process TTL cache (was scanning the whole
  inventory_cache doc on every GRN page load, once hit 46.7s/502); `/api/bom/connection-status`
  no longer requires the `bom_explorer` page permission (was falsely showing "SAP Disconnected" for
  GRN-only staff); SAP SOAP fault messages now extract just the `<faultstring>` instead of dumping the
  raw XML envelope; "movement skipped" vs "movement pending" copy fixed for the `not_applicable` case.
- Test fixtures: vendor `S9999` / `vendor1@testco.com` password changed to `DummyTest123`; fixture open
  POs `TESTGRN1` (2 items) + `TESTGRN2` (1 item) seeded for GRN testing without touching real SAP data.
  See `/app/memory/test_credentials.md`.


- **Shipment Search** (2026-08-28): added a search box next to the status/date filters on the Shipments
  page - matches on doc code (`_id`) or any item's `po_number`, combines with the existing status/date
  filters and the "N of M shipments" counter/Clear button. Client-side only, no backend change.


- **Shipment Detail Modal** (2026-08-28): clicking any shipment row (Edit button stops propagation) opens
  a modal with the full item list, created/approved/rejected timestamps + who approved/rejected, the
  rejection reason if any, and (for approved shipments) the SAP Posting Status section showing
  `sap_sync_status` plus a per-PO breakdown from `sap_gr_result.per_po` when available. Verified live via
  screenshot - correctly surfaced a real historical SAP error from before this session's
  `sap_gsa_write_client.py` namespace fix, confirming the modal reflects real stored data.


## FEATURE BATCH #2: Bulk Cart Add + Shipment History Filters (2026-08-28)

- **Bulk Cart Add**: a "Select all N items" link appears once per PO group in the dashboard table
  (only when that PO has >1 open line), toggling all of that PO's checkboxes at once (blank qty by
  default - kept consistent with the individual-checkbox behavior after code review flagged a full-qty
  auto-prefill as risky). Refactored to precompute per-PO open-item groups once (`rowsWithPoGroup`)
  instead of re-filtering on every render/click, and anchored to the first OPEN row of each group.
- **Shipment History Filters**: Shipments page now has a Status dropdown (All/In Transit/Received/
  Rejected) and From/To date filters above the table, with a live "N of M shipments" counter and a
  Clear button. Added a "Created" date column so the date filter has something visible to filter against.
- Verified via testing_agent (iteration_124, 100% pass) + a follow-up screenshot after the code-review
  polish fixes above.


## FEATURE BATCH: PO detail/search/pricing + cart-based multi-PO shipments + testing tools (2026-08-28)

- **New test login**: `test@test.com` / `Test123`, vendor_code `H1330` (same real vendor as hamidi.demo).
- **URL now shows the vendor being viewed** (user's explicit ask): `/supplier-portal/dashboard` and
  `/supplier-portal/shipments` are now `/supplier-portal/dashboard/:vendorCode` and
  `/supplier-portal/shipments/:vendorCode` - `SupplierPortalGate` in App.js redirects a bare path to the
  logged-in account's own vendor_code.
- **Testing-only vendor impersonation** (REMOVE BEFORE LAUNCH): a search box in the dashboard header
  (only rendered when `account.testing_mode` is true) lets a tester view ANY cached vendor's PO/shipment
  data. Gated end-to-end by a single flag: `SUPPLIER_PORTAL_TESTING_MODE=true` in backend/.env -> flip to
  `false` (or delete `_effective_vendor_code`'s override branch in server.py + the search UI in
  `SupplierDashboardPage.jsx`) to fully remove before going live. New `GET /supplier-portal/testing/
  vendor-directory?q=` endpoint (404s when the flag is off) backs the search, sourced from
  `supplier_portal_po_cache.vendor_name` (captured from SAP's `ShipFromLocation` on each PO item).
- **PO table**: added a search box (PO #/item #/description, client-side filter over the already-cached
  rows) and four new columns - `Unit Price`, `Subtotal` (SAP's `NetUnitPrice`/`NetAmount`), `PO Date`
  (SAP `SystemAdministrativeData/CreationDateTime`), `PO From` (buying entity name - `PartyBuyerPartyKey/
  PartyID` mapped via `sap_po_client.buyer_entity_name()`: `RI` -> "RAY INTERNATIONAL", else -> "RADISH
  TECHNOLOGIES", same 2-entity mapping already used in `stock_transfer_service.py`). Clicking a PO number
  opens a detail modal with all of that PO's line items + a computed PO Total.
- **Shipment creation redesigned into a cart flow** (user's explicit ask): checkbox + qty per PO line
  item builds a cart that can span MULTIPLE POs at once -> floating "Review Shipment" bar -> Review
  dialog (editable qty/remove) -> separate "Generate Shipment Code?" confirmation dialog -> only then
  POSTs and shows the code. Backend schema changed: `supplier_portal_shipments.items[]` now each carry
  their OWN `po_number` (was a single top-level `po_number` covering all items) - 6 pre-existing legacy
  shipment docs were migrated (top-level `po_number` backfilled onto each of their items).
- **New Shipments page** (`/supplier-portal/shipments/:vendorCode`, `SupplierShipmentsPage.jsx`): lists
  all the vendor's shipments; any still `in_transit` shows an Edit button (add items via a search against
  open POs, remove items, change qty, behind its own "Save these changes?" confirm) via new
  `PUT /api/supplier-portal/shipments/{doc_code}`. Locked (no Edit button) once Approved or Rejected by
  internal staff - enforced server-side in `update_shipment_items`, not just hidden in the UI.
  `approve_shipment` now groups items by `po_number` and posts ONE Goods Receipt call per distinct PO
  (a shipment can span multiple POs).
- **Bugs found + fixed by testing_agent (iteration_123), same session**:
  - `sap_gsa_write_client.py`'s SOAP envelope template was missing the `namespace` kwarg on `.format()`,
    so EVERY GRN approval's SAP-posting attempt threw a local `KeyError` before even reaching SAP
    (pre-existing bug, unrelated to this session's other changes, surfaced by this feature's testing).
    Fixed - confirmed live: the request now actually reaches SAP (got a real SAP-side 500 back, which is
    the separately-tracked "STILL OPEN/UNVERIFIED" SAP write item, not a local error).
  - `buyer_entity_name()` fell back to the raw buyer code instead of "RADISH TECHNOLOGIES" for any
    unmapped/blank code - fixed.
  - Cart/edit quantity inputs pre-filled with the FULL remaining qty by default - changed to blank, so a
    vendor must type an intentional quantity.
  - Testing-mode vendor impersonation only worked for `GET` endpoints - `POST /shipments` now also
    honors `?as_vendor=` (gated the same way); `PUT /shipments/{code}` needed no change since it already
    scopes to the shipment's own stored `vendor_code`, not the account's.
  - Vendor code/company name were hidden on narrow screens in the dashboard header - now always visible.


## CRITICAL BUG FIXED #2: supplier PO fetch was serving a 2-year-old stale batch, then background-cache refactor (2026-08-28)

- **User report** (persisted even after the cross-vendor leak fix below): "the pos that loaded for hamidi do not seem to be correct" / "I do not see right POs in supplier portal for the vendor Hamidi Exports."
- **Root cause #1**: `QueryPurchaseOrderQueryIn` ignores any ordering/pagination hint and always returns
  records starting from the LOWEST `PurchaseOrderID` first. With the fetch capped at `FETCH_LIMIT=500`
  (communication-timeout constraint), every single fetch - for every vendor - only ever saw the OLDEST
  ~500 open POs tenant-wide (confirmed live: PurchaseOrderID 3927-4426, all 2024-05 delivery dates, even
  though it's Aug 2026). Hamidi's real current open POs (ID ~28000+) were never reachable.
- **Fix**: confirmed live that `<SelectionByID>` (unlike `SelectionBySellerPartyID`) IS honored by this
  tenant (`IntervalBoundaryTypeCode=8`, "greater than"). `sap_po_client.py` now queries
  `PurchaseOrderID > watermark`, where `watermark` is a small Mongo-persisted pointer
  (`sap_po_watermark`) kept just behind the tenant's current max PO ID. A cheap probe (binary/
  exponential search, limit=1 each) discovers the current max ID only when the watermark is
  missing/stale (3+ days).
- **Root cause #2**: a single fetch (discovery probe + the 500-record batch) takes 60-100s+ - too slow
  for a live HTTP request; it 502'd through the Kubernetes ingress when tried inline.
- **Fix**: refactored to the SAME background-cache pattern already used by `bom_cache_service`/
  `inventory_service`/`company_cache_service`. New `start_supplier_po_cache_refresh_loop` (server.py,
  every 10 min) calls `sap_po_client.fetch_recent_window(db)` ONCE for ALL vendors (SAP doesn't filter
  by seller anyway), then `supplier_shipment_service.refresh_all_vendor_caches(db, rows)` fans results
  out into each vendor's `supplier_portal_po_cache` rows. `GET /api/supplier-portal/purchase-orders`
  now ONLY reads Mongo (no live SAP call) - confirmed ~0.3-0.4s response, no timeout risk.
- **Hardening from testing_agent iteration_122** (all fixed same session):
  - Watermark is now monotonic - an empty/no-progress SAP batch no longer walks `max_po_id` backwards
    (would have slowly regressed to the original "oldest POs" bug over repeated empty cycles).
  - `live_sync` in the API response now reflects actual freshness (`watermark.updated_at` within 2x the
    refresh interval), plus a new `last_synced_at` ISO field - previously it just meant "a watermark doc
    exists once," which could stay `true` forever even if the background loop silently stopped working.
  - Cache rows are now tagged `source="sap_live"` and only `source="sap_live"` rows are deleted during
    a refresh - protects manually-seeded demo/test fixture rows (e.g. dummy vendor `S9999`) from being
    wiped out by the global background refresh every 10 minutes.
  - Blank (`" "`) `product_id` values from SAP are normalized to `None` on ingest.
- **Verified live**: Hamidi (H1330) now correctly sees exactly 3 real CURRENT open POs (28792, 28833,
  28897; 10 line items; Aug/Sep 2026 due dates) instead of the stale 2024 batch. Dummy vendor `S9999`
  still shows its fixture data without being wiped by the background loop.
- **Known pre-existing issue, unrelated to this fix** (see `Issue: Background Job Throttling` below):
  right after a backend restart, several background refresh loops (BOM cache ~4692 products, inventory
  cache, SAP valuation lookups against a currently-flaky `my431827.businessbydesign.cloud.sap` host)
  can saturate Python's default asyncio thread pool (12 workers) for 2-3 minutes, causing transient
  502s/hangs on ANY endpoint. Self-resolves; not caused by the new supplier-PO loop specifically but it
  adds one more periodic consumer to the same pool - still P1/not started, see backlog.


## CRITICAL BUG FIXED: cross-vendor PO data leak (2026-08-26, same day)

- **User report**: "the POs loaded for Hamidi do not seem correct."
- **Root cause**: `QueryPurchaseOrderQueryIn`'s `SelectionBySellerPartyID` filter is silently ignored by
  this SAP tenant - EVERY vendor code (including a deliberately fake one) returned the exact same
  unfiltered batch of open POs spanning dozens of unrelated suppliers. The Hamidi demo login was
  actually showing "Ganesh Steel Industries" (G1287) and ~50 other suppliers' real purchase orders -
  a genuine cross-vendor data leak, not just cosmetically wrong demo data.
- **Fix**: `sap_po_client.py` now treats the SAP-side selection as best-effort only and enforces the
  REAL filter client-side by comparing each returned PO's `PartySellerPartyKey/PartyID` to the
  requested `vendor_code` before including any of its items. Verified live: H1330 (Hamidi) now
  correctly returns only Hamidi's own 73 open line items (fasteners/hardware, consistent with their
  business), and a deliberately fake vendor code now correctly returns 0 items.
- **Known remaining limitation** (flagged, not silently hidden): since SAP won't filter server-side, we
  pull a bounded batch (`FETCH_LIMIT=500`, ~60-90s per call) of the WHOLE tenant's POs and filter
  client-side - this is a scan, not a guaranteed-complete query. If the tenant's total PO volume grows
  well past ~500, some of a vendor's older open POs could theoretically fall outside the scanned batch.
  Long-term fix if this becomes a real problem: switch to an Analytics OData report (same pattern
  already used for HSN Code / On-Hand Inventory) which CAN filter + paginate server-side properly.
- Demo login `hamidi.demo@vendorportal.test` / `HamidiDemo123` (vendor_code H1330) now shows correct,
  real Hamidi Exports-only data.


## Supplier Portal Phase 3 + 4 + JDE Oracle theme + Phase 1 goes LIVE (2026-08-26, continued)

- **Phase 3 (shipment 2-way match)**: vendor picks a PO line item, ships a qty validated against remaining open qty (tracked via `supplier_portal_shipments` + `_shipped_qty_so_far` aggregation, vendor-scoped), gets an exclusive 6-char alphanumeric doc code (ambiguous 0/O/1/I excluded).
- **Phase 4 (GRN approval)**: `/admin/grn-approval` - staff lookup by code, physically match goods+invoice, Approve/Reject. Approve ALWAYS marks internal status="approved" regardless of SAP connectivity (never blocks staff), and separately attempts a real Goods Receipt post to SAP (Quality Inspection stock) via `sap_gsa_write_client.py`, tracked as `sap_sync_status: "pending"|"posted"`.
- New page permission `supplier_portal_admin` also gates `/api/admin/grn/*` (same internal team as vendor-onboarding approvals).
- `testing_agent` iteration_121: 100% pass; fixed 3 hardening items (vendor-scoped shipped-qty match, broadened exception guard around SAP posting so no surprise ever blocks internal approval, humanized GRN status badges).
- **JDE Oracle theme applied** (user's explicit ask) to all 6 new Supplier Portal/GRN pages per `/app/design_guidelines.json` (Oracle JD Edwards EnterpriseOne/Alta UI look): IBM Plex Sans (`.font-sans`)/JetBrains Mono (`.font-data`), `#0076CC` brand blue, dense flat `rounded-sm` tables/badges/buttons, `#F5F6F7` page bg. Internal pages only restyled their own content (NavTabs left untouched).
- **Phase 1 went LIVE same day**: user supplied real `SAP_SOAP_PO_ENDPOINT` (QueryPurchaseOrderQueryIn) + `SAP_SOAP_PO_MANAGE_ENDPOINT` (ManagePurchaseOrderIn - provided but unused, see why in `sap_po_client.py`). Live-verified: this tenant's query response already includes full line-item detail (no 2nd Read call needed), and portal `vendor_code` = SAP `PartySellerPartyKey/PartyID` (confirmed against real supplier G1287, 66 real open items returned). "Open" = `DeliveryProcessingStatusCode != "3"`. End-to-end tested live (real PO fetch + real shipment creation) then fully cleaned up (test account reverted to dummy vendor_code `S9999`, the live-PO test shipment + G1287 cache deleted) so the test account no longer has access to the real vendor's live data.
- **Phase 4 SAP posting still blocked**: needs a NEW write-capable GSA endpoint (`SAP_SOAP_GSA_WRITE_ENDPOINT`, distinct from both PO endpoints above and from the existing read-only GSA endpoint) + `_EMERGENTBOM` write authorization grant.
- Not built yet: Phase 5 (QMS API-key feed).


## Supplier Portal Phase 1 + 2 (2026-08-26) - external vendor onboarding + JWT auth, SAP PO fetch boilerplate

- **New product line**: a 5-phase external Supplier Portal (separate from the internal Entra ID SSO app) - this session built Phase 1 (SAP PO fetch client, boilerplate only) + Phase 2 (vendor onboarding/auth, fully working end to end).
- **Phase 2 (done, live-tested)**: `/supplier-portal/signup` (vendor_code, company_name, email, password, GST/PAN numbers + GST/PAN file uploads via Emergent Object Storage) -> status "pending" -> internal staff with the new `supplier_portal_admin` page permission approve/reject at `/admin/supplier-portal-approvals` (view uploaded docs, approve/reject with reason) -> vendor logs in at `/supplier-portal/login` and lands on `/supplier-portal/dashboard` (approved) or a Pending/Rejected screen (not yet approved, reason shown if rejected).
- **Own JWT auth** (`supplier_portal_service.py`, cookie `supplier_token`) - completely separate from `vms_session`/Entra ID; `auth_service.py`'s middleware now skips the whole `/api/supplier-portal/` prefix (`EXTERNAL_PORTAL_PATH_PREFIXES`) so it never demands an Entra login. Frontend uses a dedicated axios instance (`lib/supplierPortalApi.js`) to avoid the internal app's global 401->Microsoft-redirect interceptor firing on a wrong vendor password. `App.js` restructured (`AppShell`) so `/supplier-portal/*` renders entirely outside the internal `AuthGate`.
- **Phase 1 (SAP PO fetch) - intentionally boilerplate only**: `sap_po_client.py`'s `get_open_pos_for_vendor()` raises `SAPPurchaseOrderNotConfiguredError` (surfaced as a friendly "SAP Connection Coming Soon" on the vendor dashboard) until the user provides the real `SAP_SOAP_PO_ENDPOINT` (currently blank in `.env`) - swap in the real SOAP/OData call then, no other wiring needed.
- **Object storage**: new `object_storage_service.py` (generic put_object/get_object wrapper, reusable for any future upload feature), path convention `sap-mrp-portal/supplier-docs/{account_id}/{gst|pan}.{ext}`.
- **testing_agent (iteration_120)**: found + fixed 2 real bugs - (1) brute-force lockout was keyed on `request.client.host`, which is a rotating k8s ingress pod IP in this environment, so 5 failed logins never actually locked the account (re-keyed on email only, verified live: 5 wrong attempts -> 6th correctly blocked with "Too many failed attempts"); (2) a rejected vendor's `rejection_reason` was dead code - `authenticate()` blocked login entirely for `status=="rejected"`, so the frontend's Rejected screen could never be reached (fixed: rejected accounts now DO get a session/JWT like any other status - every data-bearing route already independently gates on `status=="approved"`, so this can't leak PO access - verified live: rejected vendor's login now returns `status: "rejected"` + the stored reason). Also added a `DialogDescription` to the reject dialog (a11y warning). GST/PAN doc link `data-testid`s were already present (false positive in the report).
- **Not built yet (next session, blocked on SAP endpoints from the user)**: Phase 3 (supplier views open POs, creates a shipment with a 6-char alphanumeric case-insensitive doc number, 2-way match), Phase 4 (internal GRN approval -> SAP Goods Receipt posted with "RESTRICTED" Quality Inspection status - needs research+user confirmation on the exact SAP service, e.g. `ManageInboundDeliveryIn`/`MaterialInspectionIn`), Phase 5 (API-key-secured feed for the external QMS app - GRN doc number, PO number, item code, qty, batch only, per user's explicit scope).
- Tested: self-tested via curl (full signup->pending->403-on-POs->admin-approve->doc-download->approved-login->503-not-configured->no-permission-403->internal-401-regression) + `testing_agent` iteration_120 (backend 24/25 pytest before fixes, both real bugs fixed and manually re-verified live after; frontend 100% of UI flows, one spec-gap now fixed). `/app/backend/tests/test_supplier_portal.py` (25 cases) left in place for regression.

---



- **New**: Reporting Point descriptions (e.g. "BLANK+PUCNCH", "FLAT_LANCER", "STAMP", "BENDING") now show wherever a raw RP code (RP_10/RP_20/etc.) used to be shown alone, on both `/production-confirmation` and `/admin/production-confirmation-test`. Pulled live from SAP's newly-activated standard SOAP service `ManageProductionBillofOperationsIn -> ReadProductionBillofOperations` (endpoint: `SAP_SOAP_BOO_ENDPOINT` in .env), keyed via `BillOfOperationsID` (resolved per Production Model ID through the existing custom OData `productionmodelbomemergent` service). New client `sap_boo_client.py`; new method `SAPProductionModelBomClient.get_bill_of_operations_id_for_model_id()`; new service fn `production_confirmation_service.get_reporting_point_descriptions()`; cached indefinitely in Mongo (`boo_id_cache`, `boo_descriptions_cache` - BoO structure essentially never changes once released). A model with no captured `production_model_id` (older orders) or no BillOfOperationsID just falls back to the raw RP code, unchanged.
- **Bug fix (found by user via screenshot)**: `ProductionConfirmationTestPage.js` has its own independent copy of `LastConfirmationBadges`/toasts (NOT shared with `ProductionConfirmationPage.js`) - the WIP-Clearing/FG-Movement "skipped" state fix from the previous session was only ever applied to the main page, so the test page still showed a scary red "WIP Cleared"+Retry chip for a correctly-skipped (not-yet-all-operations-finished) step. Ported the exact same fix (neutralChip, skip-aware toasts) into the test page. **Lesson for future work: any frontend fix to `ProductionConfirmationPage.js` must also be manually ported to `ProductionConfirmationTestPage.js` (and vice versa) - they are NOT shared components.** Also ported (previously main-page-only, missing from test page): quantity<=0 client validation, "Retry Now"/trigger-detail for stuck Create Order jobs. Did NOT port "Waiting Next Stage" column to the test page - it already has an equivalent "Awaiting Prev Step" column covering the same need from the other row's perspective.
- Verified live against real order 70570 (BK-0021, 3-stage test) via curl + screenshot - both fixes confirmed working with real SAP data, no live SAP writes made during verification.

---


## Bug fixes + features (2026-08-26) - Create Order "200s spin" investigated, WIP Clearing timing bug fixed, new "Waiting Next Stage" column

- **"Create Production Order spins for 200s" (PL-0037A, 10 EA)**: NOT actually hung - job runs as an intended background poll (up to 20 min). Root cause of the confusion: (1) no visibility into WHY `waiting_for_order` was taking long (SAP's "Request Production" trigger kept failing with "already requested" while the job silently kept re-polling), (2) a REAL bug found via live SAP log correlation - a transient SAP verify error (`get_requested_material`) during the self-release polling loop permanently discarded a matching in-prep order from `baseline_prep_ids`, which could doom a job to never find its own order within the 20-min window (real incident: Order 70547 for PL-0037A got silently orphaned this way). Fixed: `last_release_trigger_error` now stored on the job; a transient verify error no longer advances `baseline_prep_ids` (retried next poll instead of forgotten forever).
- **New**: `POST /production-confirmation/create-and-release-order/{job_id}/force-retrigger` - manual "Retry Now" button (frontend, appears after elapsedSeconds>=120 in `waiting_for_order`), plus an inline detail line showing live proposal ID + trigger attempt count + real SAP error.
- **WIP Clearing Run posting nothing (Lot 70563, Blanking/RP10 finished)**: confirmed via SAP support docs - a WIP Clearing Run only produces real journal entries once EVERY operation on the lot is finished; firing it after just the first op of a multi-step routing returns `WIPRunStatus=true` with ZERO journal entries (looked like a false "posted" badge). Fixed: new `_all_lot_operations_finished()` gates both the auto-fire (on task finish) and the manual "Retry" endpoint - shows a neutral "WIP Pending"/"Pending" chip (not a red Failure) when other operations are still open, with no misleading Retry button. Same gate applied to the SFG->FG Goods Movement trigger (same premature-firing risk on multi-step routing).
- **Bug fix**: `get_latest_confirmation_by_lot` 500'd the ENTIRE badge batch (`KeyError: 'rp'`) whenever any legacy confirmation doc (pre-dating the reporting_point_id field) was in the queried lot list - now skips just that doc instead of crashing.
- **New "Waiting Next Stage" column** on the open-lots table (Production Confirmation tab): shows pieces sitting in WIP between two consecutive routing operations (e.g. Lot 70563's RP10/Blanking row shows "10" waiting for Bending) - computed client-side from `total_confirmed_quantity` diffs between consecutive rows of the same lot.
- Tested via `testing_agent` (iteration_119): 100% pass, backend pytest suite (12/12) + frontend UI checks, all READ-ONLY (no live SAP writes triggered during testing - this tenant is LIVE, `SAP_GOODS_MOVEMENT_DRY_RUN=false`).
- Known nuance (not fixed, flagged by testing agent): "Waiting Next Stage" only diffs a row against the immediately-next row - on a lot where SAP's last operation (END) independently reports its own confirmed qty before the middle operation does, the number can look inconsistent. Matches the user's exact requested case (Lot 70563 RP10) correctly; revisit only if reported as wrong on a real multi-step lot.
- Pre-existing, unrelated issue noted by testing agent: for ~2-4 min after every backend restart, startup cache refreshes (SAP valuation/ERP MSSQL) can saturate the thread pool and `/api/auth/me` 502s, showing a blank page - not caused by this session, not fixed (backlog candidate).

---


## Gap fix (2026-08-25, continued) - "self-created only" now also enforced on Create Production Order's Recent Activity table

- User asked to confirm self-created-only filtering applies to BOTH pages. Found a gap: `proposal-history` (Recent Activity on Create Production Order tab) was only site-scoped, not creator-scoped - a "user" could see everyone else's activity at their bound site.
- Fixed: `get_proposal_and_release_history` now also filters to `released_by`/`actor` matching the caller's name for role=="user", same pattern as the open-lots table. admin/super_admin unaffected (see all).
- Verified live: a P1-bound test user (name matching no real actor) now gets 0 entries; a P1-bound "Ankit" test account gets only Ankit's 8 entries.
- Both Production Confirmation and Create Production Order tables are now consistently self-created-only for non-admin users, site-scoped + creator-scoped, admin/super_admin see everything on both.

---


## Removal (2026-08-25, continued) - Urgent Action Dashboard removed per user request

- User asked to remove the just-added/redesigned Urgent Action tile section entirely.
- Removed: `UrgentActionDashboard` component + its render call from `ProductionConfirmationPage.js`, the `GET /api/production-confirmation/urgent-actions` endpoint, and `production_confirmation_service.get_pending_order_releases` (dead code with no other callers).
- Kept intact: "Confirmed Today" and "Scrap (7d)" stat cards (separate feature, not part of this removal request), plus the "user sees only self-created lots" backend enforcement on the main table.
- Verified: page compiles clean, loads fine, no `urgent-action-dashboard` element present, other cards unaffected.

---


## Redesign (2026-08-25, continued) - Urgent Action tiles redesigned per user feedback

- **Tile 1 "Today Created Lot ID"** (replaces "Overdue POs" entirely): lots I created today via Create Production Order, personal to the viewer.
- **Tile 2 "Pending Lot ID"** (replaces "Pending Store Approvals"): combines my own open-not-yet-finished lots + my own Proposals stuck before ever reaching a released SAP Order (new `production_confirmation_service.get_pending_order_releases`).
- **Tile 3 "Pending Stock"**: same logic as the old "Component Shortages" tile, renamed only.
- Backend: `get_urgent_actions` rewritten around one shared open-lots fetch (personal to the viewer via `created_by` name match) instead of the old Open-PO Demand feed (dropped entirely per user's redesign ask).
- Known slowness (not a bug, matches existing P2 backlog item "Background Job Throttling"): this endpoint's live SAP open-lots call can take 30-40s in this environment before the tiles populate - same root cause as other SAP-dependent page loads.
- Verified live end-to-end (after discovering it just needed ~35s to resolve, not broken): tiles render, counts correct (3 Pending Stock items for P1), detail table + "Go to Store Approval" link work.

---


## Access control fix (2026-08-25, continued) - "user" role sees only self-created production orders

- **User's explicit ask**: on the Production Confirmation table, a plain "user" account should see only production orders THEY created; admin unaffected.
- `GET /api/production-confirmation/open-lots` now enforces this server-side for role=="user" (matches `created_by` - already joined via `_attach_order_creators` - against the caller's own name, case-insensitive), on top of the existing site-scoping. admin/super_admin see everything as before.
- Frontend: the "Show all / Show mine" dropdown is now hidden for role=="user" (moot - backend always returns "mine" for them); still available for admin/super_admin.
- Verified via curl with 2 synthetic accounts + a screenshot: a "user" whose name matched a real historical creator got 0 rows (their past orders are already closed/no longer "open", correctly excluded) and the P1 test user (never created anything) also got 0 - both expected, not bugs; admin still saw all 200 fetched rows.

---


## Feature batch (2026-08-25, continued) - Site-Scoped test account, Confirmed Today, Scrap Trend, Urgent Action Dashboard

- **Site-Scoped Production Users**: left one synthetic test account in the DB per user's explicit request ("for me to try") - `p1.floor.test@rampgroup.co.in`, role "user", bound_sites=["P1"], allowed_pages=["production_confirmation"]. Session token + browser console instructions in `/app/memory/test_credentials.md`. Real users should still log in with their real Microsoft account once, then be granted access via Access Management (no code changes needed there - bound_sites already applies generically, not just to Store Approval).
- **Confirmed Today card**: new `GET /api/production-confirmation/confirmed-today` - distinct lots with a successful confirmation since midnight IST (shop-floor's actual "today"), site-scoped for non-admins.
- **Scrap Trend (7d) card**: new `GET /api/production-confirmation/scrap-trend` - total scrap qty + count grouped by Scrap/Deviation Reason over the last 7 days, site-scoped. `log_confirmation` now also stores `site_id` (needed for this scoping - wasn't persisted before).
- **Urgent Action Dashboard**: new `GET /api/production-confirmation/urgent-actions`, rendered as a new top section on the Production Confirmation page - 3 clickable tiles (Overdue POs from the last autosaved Open-PO Demand feed pull - due_date past + qty_open>0, company-wide since the feed has no per-row site field; Component Shortages - distinct components blocking a pending Store Approval, site-scoped; Pending Store Approvals themselves, site-scoped), each expandable to a detail table, with a "Go to Store Approval" link.
- Both `open-lots` and `proposal-history` (Recent Activity, previous session's fix) plus these 3 new endpoints now consistently use the same bound_sites scoping.
- Tested live in-browser as the P1 test user (screenshot): 159 overdue POs (unfiltered, as designed), 3/13 shortages and 2/7 pending approvals correctly narrowed to P1 vs an admin session's full counts. Also curl-verified all 3 new endpoints return real, non-empty data against the live SAP-connected tenant.

---


## Access control fix (2026-08-25, continued) - Production Confirmation + Create Production table scoped to bound sites

- **User's explicit ask**: Production Confirmation table and Create Production Order table should only show what belongs to that user; admin sees all.
- Reused the existing Site Binding mechanism (`bound_sites` + `_filter_by_site_access`, already used for Store Approval) rather than inventing a new one - `GET /api/production-confirmation/open-lots` and `GET /api/production-confirmation/proposal-history` (the Create Production tab's Recent Activity list) now filter rows to the caller's `bound_sites` unless role is admin/super_admin.
- Low risk: checked current users - the only 2 "user"-role accounts don't currently have `production_confirmation` in `allowed_pages`, so nothing changes for existing accounts today; this is future-proofing for when site-scoped production users are added via Access Management.
- Verified live: admin session saw all sites (213 open-lot rows across 6 sites); a test "user" bound to P1 only saw 79 P1 rows and 8 P1 activity entries.
- Out of scope (not requested): Confirmation History / Proposal History dialogs, and the Create Order form's Site input itself, were left untouched.

---


## UI fix (2026-08-25, continued) - Scrap entry now gated + reason mandatory

- **User's explicit ask**: "Confirmed Scrap (Rejected Qty)" was a plain always-editable number input with no reason requirement. Changed to: a "This confirmation has Scrap / Rejected units (QC issue, damage, etc.)" checkbox that must be checked before the quantity input even appears; once checked, both "Scrap Quantity *" (must be > 0) and a new mandatory "Scrap Reason *" dropdown (reuses the existing Deviation Reason list - Quality Issue, Material Damage, etc.) must be filled before Confirm will submit. Unchecking resets both fields to blank/0.
- Verified live in the browser (real SAP PRD tenant, Lot 11592): checkbox toggles the section correctly, both fields render with the right labels/placeholders; dialog closed via Cancel, no data submitted.

---


## Bug fix (2026-08-25, continued) - Net Weight > Gross Weight silently clamped to "0 kg scrap" instead of blocking

- **User-reported bug** (Lot 70524, PL-0037A): the scrap-calc panel showed Gross Weight 1.315 kg / Net Weight 1.344 kg / Scrap/unit 0 kg - physically impossible (a stamped part can't weigh more than its raw blank). Root cause: `_compute_scrap_calc` clamped negative scrap to 0 via `max(0, ...)` and let confirmation proceed with a meaningless 0kg by-product post to SAP, masking the real master-data error (either PL-0037A's Net Weight, pushed to SAP Aug 24, or FLAT-PL37's BOM qty is wrong - user still needs to verify which on the shop floor).
- **Fix**: `_compute_scrap_calc` now returns `available: False` with a clear "Data error: Net Weight X kg is greater than Gross Weight Y kg..." reason whenever net > gross, instead of silently clamping. This automatically plugs into the by-product enforcement built earlier this session (both `get_scrap_calc` preview endpoint and `confirm_production`'s backend block) - confirmation is now fully blocked (HTTP 400) until the master data is fixed, per user's explicit "stop further process" ask.
- Frontend: the "unavailable" message is now a red warning box (was muted gray text) with "Cannot confirm yet: ..."; only shown when an RM component genuinely exists (never shown for assembly-only items with no by-product expected). Confirm button is now visually disabled + relabeled "Fix weight data first" in this state, not just blocked on click.
- Verified live against the real PL-0037A/FLAT-PL37 data that reproduced the bug: `scrap-calc/PL-0037A` now returns the data-error message, and `POST /confirm` for Lot 70524 returns HTTP 400 with the same message.
- Open action for user: determine which value is actually wrong (Net Weight 1.344 kg vs BOM qty 1.315 kg) and correct it - not yet resolved, only the app's response to the inconsistency is fixed.

---


## Session update (2026-08-25, continued) - FG identification + auto SFG->FG Goods Movement after Production Confirmation

- **FG identification (user question, answered)**: confirmed the app already has a deterministic rule (`bom_categorizer.py`) to distinguish a true Finished Goods item from a Sub-Assembly: has its own BOM AND is never used as a component inside any other product's BOM = FG; has its own BOM but IS used as a component elsewhere = Sub-Assembly. Verified live against real data (PL-0037 = FG, PL-0037A = Sub-Assembly, consumed inside PL-0037's own BOM).
- **New feature - auto SFG->FG Goods Movement**: right after WIP Clearing succeeds on a Production Confirmation, if the confirmed item is category "Finished Goods" (checked live via new `bom_categorizer.classify_single_product_live()` + `production_confirmation_service.get_or_classify_category()` if never categorized before, falls back to skip if genuinely undeterminable), an automatic Goods Movement posts the confirmed quantity from `{site}-FG` (was already `{site}-SFG` before movement) to `{site}-FG`. Reuses `store_approval_service._trigger_goods_movement` (same retry/dry-run/error-clarify infra as Store Approval's RM issuing). Sub-Assemblies are deliberately left untouched (stay in SFG for the next assembly step).
- New endpoints: `POST /api/production-confirmation/retry-fg-movement` (retry a failed movement, mirrors retry-wip-clearing). Confirm job now passes through `checking_category`/`moving_to_fg` phases, surfaced in the Confirm dialog's button label for step-by-step progress (user's explicit ask).
- Frontend: "FG Moved" badge + inline Retry (parallel to "WIP Cleared") in `LastConfirmationBadges`; new "FG Movement" column in the Confirmation History table.
- **IMPORTANT - live SAP disclosure**: `SAP_GOODS_MOVEMENT_DRY_RUN=false` in this environment - Goods Movements hit the real/live SAP tenant, not a sandbox. Verified via curl during self-testing (moved 1 EA PL-0037 P1-SFG->P1-FG, immediately reversed P1-FG->P1-SFG, net zero effect) - user was informed and opted to do final live-movement verification themselves in the app.
- Tested: unit-level (classify_single_product_live x3 scenarios, get_or_classify_category caching behavior) + one live end-to-end goods-movement round-trip (reversed). Frontend smoke-checked (login gate renders, no crash). User will self-test the actual production confirmation -> FG movement flow with a real lot.

---


## Session update (2026-08-25, continued 27) - By-product backend enforcement + 2 recovery UX features (WIP Retry, Order-creation Resume)

- **By-product/scrap backend enforcement** (was frontend-only, flagged in handoff as forgotten): `/api/production-confirmation/confirm` now re-validates via a shared `_compute_scrap_calc()` helper (extracted from `get_scrap_calc`) BEFORE creating the confirm job - blocks with HTTP 400 if a real RM component was found in the BOM but its weight/target output line is unavailable; correctly SKIPS the check for assembly-only items with no mass-based RM component at all (e.g. hardware kits in a bag - user's explicit concern, verified). Frontend now also sends `material_inputs` on the confirm POST so server and UI never disagree. Verified via 4 curl scenarios (no-RM passthrough, missing-net-weight block, no-target block, valid-target passthrough).
- **WIP Clearing Retry** (user's ask - stalled/unclear steps should be retryable inline): new `POST /api/production-confirmation/retry-wip-clearing` + `production_confirmation_service.retry_wip_clearing_for_lot()`. "Retry" link now appears next to a failed "WIP Cleared" chip in the Production Confirmation table's Last Confirmation column (`LastConfirmationBadges`), updates the chip in place on response.
- **Production Order creation failure recovery** (user's ask - errors mid-order-creation used to force a trip to the SAP UI): `_run_create_and_release_job`/`_continue_order_creation`'s generic except blocks now tag failures with `result.reason="pipeline_error"` + whatever `production_proposal_id`/`production_order_id` was already reached in SAP. New `POST /api/production-confirmation/create-and-release-order/{job_id}/resume` resumes the pipeline from that known Proposal ID under a fresh job_id. Frontend Active Orders table: failed jobs (other than the pre-existing `sfg_shortage` precheck, unchanged) now stay visible with a "Why this failed" reason + a "Resume" button instead of silently vanishing/mislabeling as "SFG Shortage - Blocked".
- **testing_agent (iteration_117)** caught 1 real defect: the resume endpoint had no job-status guard, allowing a resume on an already-`running`/`done` job (risk of duplicate SAP Release). Fixed - now only `status=="failed"` jobs are resumable (mirrors the existing `/cancel` guard), re-verified via curl (400 on a `running` job). Also broadened `retry-wip-clearing`'s exception handling to never surface a raw 500.
- Tested: self-tested via curl (all scenarios both before and after the fix) + testing_agent iteration_117 (91%→100% after the fix). Frontend smoke-verified, 0 console errors on both tabs.

---


## Session update (2026-08-25, continued 22) - Follow-up bug: scrap-calc/by-product panel had the SAME "wrong Production Model" guessing bug

- User caught this live: created a NEW order (lot 70422) explicitly picking BK-0021_1 (correct model, confirmed real MaterialInput = FLAT-BK21 per continued-20's fix), but the Confirm dialog's by-product/scrap-calc panel still showed "AUTO-CALCULATED FROM SH4.5HR" - the WRONG model (that's BK-0021_2's input, a different Production Model for a different site).
- **Root cause**: this panel is powered by a SEPARATE endpoint, `/production-confirmation/scrap-calc/{product_id}`, which I had NOT touched in continued-20 - it independently guessed the RM component from the same cached "highest revision" `bom_node_cache` doc (keyed by product_id alone), same root bug, different code path.
- **Fixed**: `get_scrap_calc` (server.py) changed GET->POST, now accepts the same optional `material_inputs` list (from the lot's own SAP data, already available on `row.material_inputs`) - when given, filters for KGM (mass) components directly from it instead of guessing via `bom_node_cache`, with description best-effort from `inventory_cache`. Falls back to the old cached-BOM guess only when no material_inputs are given. `MaterialInputItem` model moved earlier in server.py so both this and the earlier component-availability endpoints share one definition.
- Frontend (`ProductionConfirmationPage.js`): the Confirm dialog's scrap-calc fetch now POSTs `row.material_inputs` alongside the product ID.
- **Tested live** against real lot 70422: curl confirmed old behavior (no material_inputs) still guesses SH4.5HR, new behavior (with material_inputs) correctly resolves FLAT-BK21; screenshot of the real Confirm dialog for lot 70422 now shows "AUTO-CALCULATED FROM FLAT-BK21" with correct Gross/Net Weight and by-product quantity. Self-tested (backend+frontend, well-verified against real SAP data) - no testing_agent run.

---


## Session update (2026-08-25, continued 26) - Split "Inventory Management" into 3 independent access rights

- User's ask: separate access rights for Inter Plant Stock Transfer and Goods Issue - found Goods Issue already had its own right (`store_approval`), but Inter Plant Stock Transfer was bundled under the same "inventory" permission as Stock Overview (no way to grant one without the other).
- New page key `stock_transfer` ("Inter Plant Stock Transfer") added to `auth_service.py`'s `PAGE_CATALOG`/`PAGE_ROUTE_RULES` (covers `/api/stock-transfer/*`, plus added to the shared `/api/products/search` and `/api/store-requests/known-sites` rules it also depends on). `inventory` relabeled "Stock Overview" and `store_approval` relabeled "Goods Issue" for clarity (backend keys unchanged, no migration needed - no `role=user` account currently has "inventory" granted).
- Frontend: `NavTabs.jsx`'s "Inter Plant Stock Transfer" sub-tab now gates on `stock_transfer`; new `App.js` routes (`/inventory/inter-plant-transfer`, its delivery-note, its gate-pass) all switched from `page="inventory"` to `page="stock_transfer"`; `AuthContext.jsx` PAGE_LABELS updated to match.
- Verified live: a test `user`-role account with ONLY `stock_transfer` granted got 200 on `/api/stock-transfer/orders` but 403 on `/api/inventory` and `/api/store-requests` - confirmed fully independent. Access Management screenshot confirms 3 separate checkboxes (Stock Overview / Inter Plant Stock Transfer / Goods Issue).

## Session update (2026-08-27, continued 25) - Date auto-fill + ERP Pcode resolution + InvStk_status="Open"

- **Requested Delivery Date** now defaults to today; whatever is entered there auto-fills **Date Of Supply** (still independently editable after) - also applied to the AI Quick Entry flow and the post-submit form reset.
- **ERP Pcode fix** (found live, confirmed with user before wiring): `comp.pcode` is the ERP's own internal plant code per site (e.g. P8→"R2970", P3→"RTP3") - confirmed against real historical `DeliveryChallan` rows that Pcode = the SHIP-TO site's own `comp.pcode`, NOT our app's raw site ID (the bug). `sync_to_erp_portal` now resolves it via `company_cache_service` (already-cached, no extra live call), falling back to the raw site ID only for site P6 (has no pcode registered in `comp` at all - user's explicit choice for this gap).
- **InvStk_status fix**: `Pro_DeliveryChallan_Insert` has no parameter for this column at all (confirmed against the proc's own signature) - it was always left blank. `erp_portal_client.create_delivery_challan` now runs one extra `UPDATE DeliveryChallan SET InvStk_status='Open' WHERE Sale_No=... AND CompCode=...` right after the insert, in the same transaction.
- Also added site "W1" to `SITE_TO_COMPANY` (confirmed live via `comp` table: W1 = Company RI, was previously missing/defaulting to RT) - "P1W" (its ERP pcode, not itself a site_id) was already added last session, kept alongside.
- Self-tested live: `get_company_info` confirmed correct pcode per site (R2970/RTP3/null-for-P6/etc.), the UPDATE SQL syntax verified against a real row with an explicit ROLLBACK (no data changed), frontend date auto-fill compiled and verified in code. No brand-new STO was created end-to-end in this session (avoided an unnecessary live SAP+ERP write) - **next real STO creation will be the first live end-to-end proof of the Pcode/InvStk_status fields**; recommend a quick check after your next real order.

## Session update (2026-08-25/27, continued 24) - Performance fix: HSN Code + ERP company/address + Rate all now cached in Mongo (no more live SAP/MSSQL calls on Add Item or every Delivery Note/Gate Pass print)

- User reported: item-add on the Stock Transfer form felt slow, and asked whether stock refresh was live-per-click (it wasn't - already cache-based) or the Delivery Note/Gate Pass print was slow (it was - confirmed live in backend logs: SAP itself was timing out, and every print made 3 live external calls with zero caching).
- **HSN Code** (`hsn_cache_service.py`, NEW): persistent, no-expiry Mongo cache (`hsn_code_cache`) - once looked up for a product, cached FOREVER (HSN never changes once maintained in SAP). Wired into `get_product_stock_locations` (Add Item) and `create_stock_transfer_order`. Verified live: first lookup for an uncached product took ~8s (live SAP), second lookup for the SAME product took 0.16s (cache hit).
- **ERP company/address** (`company_cache_service.py`, NEW): Mongo cache (`erp_company_cache`) of the ERP's `comp` table, refreshed by a new background loop (startup + every 12h, `server.py`), with a live one-off fallback if a site is ever missing. `get_delivery_note_data` now reads this instead of querying MS SQL live on every print.
- **Rate/Amount** (user's explicit ask: "should be stored in mongo per record since u write to erp anyway"): `sync_to_erp_portal` now persists the computed `rate`/`amount` (SAP Moving Average price, never Standard Cost) back onto the STO's own Mongo `items` at sync time (the ONE place it's still fetched live) - `get_delivery_note_data` now reads these stored values instead of re-querying SAP Valuation on every print. **Orders synced before this fix show rate/amount as 0.00 on their Delivery Note until re-synced** (HSN was already being stored since an earlier session, so that field is unaffected for old orders).
- **Net effect**: `get_delivery_note_data` no longer takes `sap_valuation_client`/`sap_hsn_client` params at all - it's now a pure Mongo read (+ a possible one-off live fallback only if the company cache is ever missing a site). Verified live: STO-000041's delivery-note call went from being blocked/timing out (thread-pool congestion from live SAP calls) to 0.319s.
- **Other RI/RT fix**: `SITE_TO_COMPANY` (sap_wip_clearing_client.py) gained 2 more Company RI sites per user's explicit statement - P5 and P1W (previously only P1/P8) - used both for WIP Clearing eligibility and as a same-company-name fallback on the Delivery Note letterhead when a site has no row in the `comp` table at all.
- Self-tested via curl (timed before/after) + direct Mongo inspection - no testing_agent run for this specific perf fix (backend-only, low-risk, well-verified).

## Session update (2026-08-25, continued 23) - Bug fixes from testing_agent report (iteration_116) on the Freight Forwarder/Delivery Note/Gate Pass feature

- `/stock-transfer/{sto_id}/delivery-note` now returns a clean 404 (was a raw 500) for an unknown STO ID.
- P3's State/State Code showed "India"/"—" (raw `comp` table data gap) - `erp_portal_client.py` now derives both reliably from the GSTIN's own first 2 digits (official CBIC state code table added) whenever the raw column is missing or says the literal junk value "India".
- Delivery Note was printing the site's city/PIN TWICE (once embedded in the ERP's own free-text address lines, once again as a separate line) - the separate line is now dropped since every site's `Cadd1`/`Cadd2` already contains it.
- Fixed a React duplicate-key warning on the per-item source-warehouse dropdown (StockTransferPage.js) - caused by `include_non_usable` now returning 2 rows for the same warehouse_id (usable + non-usable stock at the same location); deduped by warehouse_id (usable rows sort first already, so they're kept).

## Session update (2026-08-25, continued 22) - Feature: Freight Forwarder field + redesigned Delivery Note + new Gate Pass print page

- User's asks (with explicit choices confirmed via ask_human): (1) mandatory "Freight Forwarder" field on STO creation, written to the SAP GST Note + ERP portal's `Trans` field; (2) Delivery Note now shows real per-site Company Name/Address/GSTIN/PAN (from the ERP's own `comp` table, keyed by site code) instead of one hardcoded company block - letterhead correctly swaps "RADISH TECHNOLOGIES" vs "RAY INTERNATIONAL" depending on the Ship-from site; (3) Serial Number format = "{ShipFromSiteId}-{Sale_Noc}-{session}" (session = ERP's own fiscal-year code, e.g. "2627"); (4) new standalone printable Gate Pass page (Serial No/Date/Gate Pass Type/Customer=Ship-TO company/Vehicle No/Transport No=Freight Forwarder or "Self", plus a CODE128 barcode of "Sale_Noc:Comp_code:Sale_no:C" via the new `jsbarcode` package).
- Backend: `erp_portal_client.get_company_info()` (new), `StockTransferOrderCreate.freight_forwarder` (new required field), `_build_gst_note_text`/`sync_to_erp_portal`/`get_delivery_note_data` all updated (see continued-24 above for the later caching follow-up to this same code).
- Frontend: `StockTransferPage.js` (new mandatory field + validation + confirm/detail display), `DeliveryNotePage.js` (fully rewritten), `GatePassPage.js` (new), `Barcode128.jsx` (new), new route `/inventory/inter-plant-transfer/:stoId/gate-pass`.
- Tested via `testing_agent` (iteration_116, mostly PASS - see continued-23 above for the bugs found+fixed) plus extensive live curl/screenshot verification by main agent against real STO-000041.

## Session update (2026-08-25, continued 21) - Production Model ID now shown on Production Confirmation + Proposal/Order History tables

- User's ask: "I need to identify which Production Model I used to create a production order" - the Production Model picked in the Source of Supply picker at order-creation time was never persisted anywhere.
- **`CreateProductionProposalRequest`** (server.py) gained `production_model_id` (human-readable, e.g. "BK-0021_1", alongside the existing UUID) - frontend now sends `selectedSosOption.production_model_id` on order creation.
- **`log_proposal_creation`** now saves it on the `production_order_creation_history` doc; **`get_proposal_and_release_history`** and **`get_order_creators`** (the join used by `_attach_order_creators` for the open-lots list) both now surface it.
- **Frontend**: new "Production Model" column on both the main Production Confirmation grid (joined by Production Order ID, same mechanism as "Created By") and the Proposal/Order History table. Orders created before this fix show "—" (expected, confirmed with user).
- **Tested live**: simulated a proposal+release history row end-to-end via direct service calls (confirmed `production_model_id` flows through both `get_proposal_and_release_history` and `get_order_creators`), then verified in the real UI against real lot 70411 - "Production Model" column correctly shows "BK-0021_1" on both tables, no layout issues (screenshot-verified). Self-tested (small backend+frontend change) - no testing_agent run.

---


## Session update (2026-08-27, continued 20) - Production Confirmation shortage check now uses the LOT'S OWN exact SAP MaterialInput (no more BOM guessing)

- **Root cause, fully resolved**: `check_component_availability`'s cached "highest revision" BOM guess (and even its newer site-scoped Source-of-Supply auto-resolve) could still pick the WRONG Production Model when SAP has 2+ active models for the same product (real case: BK-0021 at P2 needs FLAT-BK21, but the cache/guess kept resolving to SH4.5HR from a different site's model). SAP's own Production Lot XML (`QueryProductionLotISIIn`) already returns a `<MaterialInput>` block per ConfirmationGroup with the EXACT components that specific lot was planned against - no ambiguity possible, since it's the lot's own already-resolved data, not a guess.
- **`sap_production_lot_client.py`**: `_parse_lot_block` now also parses `MaterialInput` blocks (ProductID, PlannedQuantity/unitCode, TotalConfirmedQuantity, SourceLogisticsAreaID) per group, scales each to a per-output-unit `qty_per_unit` ratio (planned_quantity / that Reporting Point's own PlannedQuantity), and attaches as `material_inputs` on every row - flows automatically through both `find_open_lots` and `find_lot_by_id` (shared parsing code).
- **`production_confirmation_service.py`**: new `check_component_availability_from_material_inputs()` + `_check_availability_against_material_inputs()` - same live-SFG-stock-first/cache-fallback shape as the old BOM-based check, but sourced directly from the lot's own `material_inputs` (descriptions best-effort from `inventory_cache`, since MaterialInput itself carries no description and these products often aren't in any cached BOM at all - e.g. FLAT-BK21 was in NO local `bom_node_cache` doc, explaining why the old guess had no way to find it). `check_component_availability_batch()` now uses this path whenever a row carries `material_inputs`, falling back to the old cached-BOM guess only for rows that genuinely have none.
- **`server.py`**: `/production-confirmation/component-availability` changed from GET to POST (now accepts an optional `material_inputs` list); `/component-availability-batch` rows gained the same optional field. Both fall back to the old guessing behavior if not provided (defensive, not expected in practice going forward).
- **Frontend (`ProductionConfirmationPage.js`)**: Confirm dialog's live availability check and the open-lots list's batch stock badges now both send `row.material_inputs` (already present on every row returned by SAP) instead of relying on the backend to guess.
- **Tested live** against real lot 70411 (BK-0021 @ P2, previously mis-resolved to SH4.5HR): `GET /production-confirmation/lot/70411` now returns `material_inputs: [{product_id: "FLAT-BK21", qty_per_unit: 2.017, ...}]` (correct model); `POST /component-availability` with that exact list returns FLAT-BK21 with correct description ("HR FLAT SIZE...") and live SFG stock (28,846.13 kg available vs 20.17 kg required, sufficient=true); batch endpoint verified both with and without `material_inputs` (fallback path still works). Backend/frontend both compiled cleanly. Self-tested via curl (backend-only logic fix, well-verified against real SAP data) - no testing_agent run.
- **P0 issue closed.**

---


## Session update (2026-08-25, continued 19) - ERP portal CompCode fix + Delivery Note Serial Number fix

- Bug: `sync_to_erp_portal()` was writing `CompCode` as the Site->Company mapping (RI/RT) instead of the actual Ship-from Site ID - user's explicit fix ("RI/RT does not need to go to ERP"). Now writes `doc["ship_from_site_id"]` (e.g. "P3") directly.
- Delivery Note (`DeliveryNotePage.js`) Serial Number now shows only `erp_sale_noc` (was showing `erp_sale_no / erp_sale_noc`).
- Both fixes only apply to NEW STOs going forward - existing already-synced legacy ERP rows still carry the old RI/RT CompCode (not retroactively corrected, not requested).
- Self-tested (small backend/frontend fix), both compiled cleanly.

---


## Session update (2026-08-25, continued 18) - Gross Weight wired to SAP + full child-level data correction (PRODUCTION incident - shared MongoDB/SAP with preview)

- User reported on PRODUCTION (mrp.radishtechnologies.com) that Net Wt./SA showed 0 locally for MSPIPE20X30 despite SAP having real values. Confirmed preview and production share the SAME MongoDB + SAP tenant (not separate envs) - this was a direct consequence of the earlier "wipe all historic data" cleanup, which correctly wiped a value that (per user) was never actually a legitimate field for Coil/Sheet/Pipe raw materials in the first place. User will provide a fresh corrected Excel for Parent-level Net Wt/SA going forward.
- **Gross Weight now fully wired to SAP** (user's explicit "yes wire gross weight"): live-confirmed a NEW custom field `ItemGrossWeightcontent_KUT`/`ItemGrossWeightunitCode_KUT` now exists on the `materialgeneralinfo` OData service (added by SAP admin, wasn't there before). Added `gross_weight_kg` to `PHYSICAL_FIELD_CONFIG` in `sap_material_physical_client.py` - since `get_physical_attributes`/`push_physical_attributes`/`bulk_push_physical_to_sap` all loop over this config generically, Gross Weight automatically got full read+write support with no other code changes (just removed the stale "(local only, not pushed to SAP)" UI caption and updated `bulk_push_physical_to_sap`'s Mongo query to include it).
- **Full data correction** using user's 2nd corrected Excel (467 rows, same Parent/Product ID structure, now explicitly showing children's Net Wt./SA should be 0): for all 467 "Product ID" (child) materials - cleared `net_weight_kg`/`surface_area_sqin` locally (unset) AND pushed 0.00 to SAP (the practical way to blank these Edm.Decimal fields, confirmed via `get_physical_attributes`'s own `!= 0` filter), while pushing each child's correct Gross Weight live to SAP for the first time. Live-verified: SAP had actually held wrong contaminated values on children from a much earlier, pre-this-session bug (e.g. `COIL.95X103` had `0.18kg/77.42in²` matching its PARENT's values) - now cleared, only Gross Weight remains.
- **Tested**: 467/467 pushed successfully (433 first pass + 34 transient failures retried, all succeeded on retry - connection timeouts/SAP locks, not logic errors). Live read-back confirmed on multiple materials (COIL.95X103, MSPIPE20X30, HRCOIL2.8158.6, MSPIPE402015 etc.) - correct final state on all. This was a live production data correction executed directly per explicit user instruction, not a simulated test.

---


## Session update (2026-08-27, continued 17) - Weight/Surface Area bulk import bug fixed + full data correction + live SAP push

- **Root bug fixed**: the Excel bulk import in `AdminPage.js` only read a single "Product ID" column and wrote Net Wt./Gross Wt./SA all to that one target - ignoring "Parent Product ID" entirely. Per user's explicit spec: Parent Product ID must get Net Wt. + Surface Area, (child) Product ID must get Gross Wt. only. Fixed - now reads both columns and routes each field to its correct target document (2 separate PATCH calls per row where applicable).
- **Historic contamination found and corrected**: all 236 components that had Net Wt./SA set locally had ALSO already been live-pushed to SAP - meaning real SAP materials likely held wrong values (written under the old buggy "Product ID" - only logic). Per user's explicit instruction ("we consider them all buggy" / "remove historic data"), wiped `net_weight_kg`/`surface_area_sqin`/`sap_physical_pushed_at` from ALL 3391 `component_master` docs.
- **Re-imported the user's correct source file** ("gross net sa bulk.xlsx", 467 rows) directly via script using the exact same logic as the fixed Admin UI import: Parent Product ID -> net_weight_kg + surface_area_sqin, Product ID -> gross_weight_kg.
- **Pushed all 466 Parent Product IDs' Net Wt./SA live to SAP** via `bulk_push_physical_to_sap` (same function the "Push All to SAP" button uses) - **100% success, 0 failures**. Live read-back confirmed correct on real SAP records (e.g. 100002693-A: net_weight_kg 0.18, surface_area_sqin 77.42 - exact match to source Excel).
- Note: accidentally launched several duplicate concurrent push runs due to a transient tool infra issue (background job launches silently succeeded despite tool-side "context deadline exceeded" errors) - killed extras, verified final DB/SAP state is fully consistent (466/466, no corruption, idempotent PATCHes).
- **Tested**: live SAP read-back on 2 real materials confirmed correct values. This was a live production data correction (not a simulated test) - executed directly per explicit user instruction.

---


## Session update (2026-08-27, continued 16) - Same lowercase bug fixed for Part Code too

- Same class of bug as the site-code fix above, but for `product_id`: a lowercase part code (via AI Quick Entry OR direct API) failed to match the inventory cache (Mongo exact-match is case-sensitive) - silently added an empty/broken line (no description, no locations, no HSN) rather than erroring.
- Fixed at the single shared source: `get_product_stock_locations()` and `create_stock_transfer_order()` now both normalize `product_id` to uppercase before any lookup - covers AI Quick Entry, manual form entry, and direct API calls in one place.
- **Tested**: live call `get_product_stock_locations(db, "scr755wm", True)` -> correctly resolved to `SCR755WM` with full description + 8 stock locations. Self-tested (small backend fix).

---


## Session update (2026-08-27, continued 15) - AI Quick Entry lowercase site bug fixed

- Bug: typing a lowercase site (e.g. "p3" instead of "P3") in AI Quick Entry free text silently failed to populate Ship-to Site - the LLM just echoed back whatever case the user typed, which then didn't match the uppercase dropdown option value.
- Fix: `parse_natural_language_transfer_request()` now case-insensitively matches the LLM's returned `ship_to_site_id` against the real `known_sites` list and returns the correctly-cased value.
- **Tested**: live call with input "transfer 500 of SCR755WM to p3 by tomorrow" -> correctly returned `ship_to_site_id: "P3"`. Self-tested (small backend fix).

---


## Session update (2026-08-27, continued 14) - Delivery Note polish: DM Sans font, INR currency, Retry ERP Sync button

- **Delivery Note (`DeliveryNotePage.js`)**: switched font from generic serif to DM Sans (user's ask - Bogle isn't licensable/available on any CDN, DM Sans chosen as closest free alternative); added ₹/INR currency labels on Rate/Amount/Total columns and "Indian Rupees" wording in the amount-in-words line.
- **"Retry ERP Sync" button** (user's explicit ask - "no legal document can be created" while ERP sync is stuck failed, since the Delivery Note's Serial Number comes from the ERP portal's own Sale_No): new `reset_erp_portal_sync_for_retry()` in `stock_transfer_service.py` + `POST /stock-transfer/orders/{sto_id}/retry-erp-sync` in `server.py`, reusing `erp_portal_client`'s existing primary->fallback host failover. Guarded against double-posting: rejects retry unless `erp_portal_status == "failed"` (verified live - correctly blocks retry on an already-"synced" order to prevent a duplicate Delivery Challan/Sale_No in the legacy portal). Button + "Retrying..." badge added to both the order detail modal and Recent Orders table.
- **GR No. field**: now strips non-digit input as-typed + blocks form submission with a clear error if non-numeric (user's ask - "gr number is numeric only").
- Also fixed the small bug where an already-flagged `erp_portal_status: "retrying"` status now displays correctly on both the Recent Orders table badge and the detail modal.
- **Tested**: direct python test confirmed retry guard rejects a synced order and correctly resets a failed one to "retrying"; both frontend/backend compiled cleanly. Self-tested (small/medium UI+backend changes).

---


## Session update (2026-08-27, continued 13) - Price + HSN added to the SAP-side GST Note; validated Delivery Challan does NOT get these fields

- **Validated live** (user's ask): Vehicle No/GR No/Place of Supply/Date of Supply/Price/HSN do NOT land on the actual SAP Delivery Challan screen (`OutboundDeliveryCollection`) for our automated orders - checked 3 real just-created deliveries (P2D1-5385, P2D1-5386, P3D1-2409): `VehicleNo_KUT`, `GRNo1_KUT`, `PlaceOfSupply_KUT`, `DateOfSupply_KUT` all blank, confirming the known SAP scheduler-lockout limitation from an earlier session still holds. Only the Note on the Customer Requirement carries this info.
- **Added Price + HSN to that same Note** (user's explicit ask - "price needs to go with hsn on the note"): new `_price_hsn_for_note()` in `stock_transfer_service.py` does a live SAP Moving Average cost lookup (reuses `sap_valuation_client`) paired with each item's already-fetched `hsn_code`, formatted as `"Items: SCR755WM HSN 73181500 Rate 2.81"` appended to the existing GST/Transport note text (verified live, real order STO-000037: full note = "GST/Transport Info - Mode: By Road; Vehicle No: UP81AA0099; ... | Items: SCR755WM HSN 73181500 Rate 2.81", 166 chars, well under SAP's 1000-char note limit).
- `submit_order_to_sap()` now takes `sap_valuation_client` param; `server.py`'s `_run_submit_sto_to_sap_job` passes the existing global client through.
- **Tested**: direct python call against real STO-000037 confirmed correct note text and cost lookup. Self-tested (small, well-verified backend change).
- Note: this Note-based workaround is confirmed the ONLY place in SAP itself these 7 fields (5 GST + price + HSN) are visible - landing them on the actual Delivery Challan screen fields would need a Basis-side Key User Extension/BAdI in ABSL (Cloud Applications Studio), outside what OData/SOAP automation can do.

---


## Session update (2026-08-27, continued 12) - Cross-site GI root cause RE-DIAGNOSED and FIXED; misleading suggestion text fixed; Refresh Site Stock button added

- **Cross-site GI bug (was mis-diagnosed as "missing Logistics Model" in an earlier session) - now genuinely FIXED, zero code changes needed:**
  - User pushed back hard that this felt like an app bug, not SAP setup. Live-walked them through SAP UI step by step and found the REAL cause: every site (P1/P2/P3/P8/P9...) already has its own `SHI_<site>` Standard Shipping Logistics Model (contradicts the earlier "missing model" diagnosis) - but P1/P2's Material Flow rule (Supply Chain Design Master Data -> site -> "Material Flow" tab) had a restrictive Basic/Advanced Rule limiting Site Logistics to only 1-2 specific Logistics Areas (P1: only FG+RM: P2: only FG) while P8's equivalent rule is completely EMPTY (no restriction at all, works for any warehouse).
  - User removed the restrictive Source Determination Rule rows for P1 and P2 (matching P8's empty rule) directly in SAP. Re-tested live immediately both times via direct `PGIInBackground` calls against real previously-stuck orders: **STO-000026 (P1) and STO-000029 (P2) both now return HTTP 200 and `order_fulfilment_status: "3"` (Finished)** - confirmed fixed live, DB records updated (`gi_status: "posted"`).
  - P3 and P9 still pending the same manual fix from the user (same steps, same expected result) - not yet done as of this update.
  - **No app code changes were needed or made for this fix** - purely a SAP master-data change.
- **Stock Transfer form UX fixes (`StockTransferPage.js`)**:
  - Fixed misleading "No stock at {site} for this item" message - it was firing whenever the AI's single "best" (highest-qty) pick differed from the chosen ship-from site, even when that site DID have enough usable stock (just not the single highest company-wide). Now only shows when there is genuinely zero usable stock at the chosen site.
  - Added "Refresh Site Stock" button + site selector next to the line items table (user's explicit ask - after a live SAP Goods Movement, the cached quantities weren't updating without waiting for the scheduled refresh). New `refresh_stock_quantities_for_site()` in `inventory_service.py` (site-scoped live SAP pull, reuses `_refresh_stock_quantities_scoped`), new job-based `/stock-transfer/refresh-site-stock` POST + GET endpoints in `server.py` (same async job pattern as `refresh-live-sfg-stock`). On completion, frontend re-fetches inventory for every already-added line item.
- **Tested**: backend service-layer calls verified live (`refresh_stock_quantities_for_site(db, client, "P3")` -> `rows_found: 899`); webpack compiled cleanly. Self-tested (small/medium UI+backend change) - no testing_agent run.
- **Next**: user still needs to remove the restrictive Material Flow rule for P3 and P9 (same steps as P1/P2) to fully close this issue.

---


## Session update (2026-08-24, continued 11) - HSN Code shown live on the Stock Transfer form

- User's ask: show the live SAP HSN Code on the form itself as it populates (not just backend ERP sync).
- **Backend**: `stock_transfer_service.get_product_stock_locations()` and `create_stock_transfer_order()` now take an optional `sap_hsn_client` and return/persist `hsn_code` per item (live SAP lookup, batched at order-creation time). `server.py`'s `/stock-transfer/inventory` GET and `/stock-transfer/orders` POST now pass the global `sap_hsn_client`.
- **Frontend (`StockTransferPage.js`)**: line item table shows "HSN: 7318" (teal) or "HSN: Not maintained in SAP" (grey) under each product as soon as it's added to the form. Order detail modal + Recent Orders items table gained an "HSN Code" column.
- **Tested**: direct python call to `get_product_stock_locations(db, "P27175", True, hsn_client)` confirmed `hsn_code: "7318"` returned (matches live SAP), backward-compat call without the client confirmed `hsn_code: None`. Frontend webpack compiled cleanly, no JSX errors. Self-tested (small UI+wiring change) - no testing_agent run.

---


## Session update (2026-08-24, continued 10) - HSN Code: SOLVED, live, zero remaining Basis blocker

- Prior blocker: `HSNCodeIndia` (Material BO) and `INHSNCode` (CustomerInvoice/APCI_CUSTOMER_INVOICE BO) are both PSM-blocked from custom OData services on this tenant - confirmed dead ends via extensive user-guided SAP UI exploration this session (custom BO service picker genuinely has no HSN field on either BO).
- **Found the real working path**: Business Analytics -> Design Reports -> new report on the "Material Master Data" data source DOES expose HSN Code as a normal reportable characteristic (`CGLO_IN_HSN_CODE`, alongside `CMATR_INT_ID` for the Material ID) - same analytics-report-as-OData mechanism this app already uses for on-hand inventory (`sap_inventory_client.py` / `SAP_INVENTORY_ODATA_URL`). User built the report ("MATERIAL MASTER HSN") in the SAP UI and generated its OData query URL via the report's own "Generate Data Query" button.
- **Verified live** (Aug 27 2026): filtering this report by `CMATR_INT_ID eq '<product_id>'` (unlike sap_inventory_client's `$select`-only quirk) correctly resolves the real business Material ID and returns that material's live HSN code, e.g. `P27175 -> "7318"`.
- **Implemented**:
  - New `sap_hsn_client.py` (`SAPHSNClient.get_hsn_codes(product_ids)` -> `{product_id: hsn_code}`, batched OR'd `$filter` same pattern as `sap_valuation_client.py`).
  - `.env`: `SAP_HSN_ODATA_URL` added (report `RPZ0947E2F59B1F629245246D`), reuses existing `SAP_ODATA_USERNAME`/`SAP_ODATA_PASSWORD`.
  - `stock_transfer_service.sync_to_erp_portal()` now takes a `sap_hsn_client` param and fills each line's `hsn_no` from a live lookup instead of hardcoded `None`.
  - `server.py`: instantiates `sap_hsn_client`, passes it through `_run_erp_portal_sync_job`.
- **Tested**: live python script hitting the real SAP report + a real existing STO (STO-000035, product P27175) - HSN resolved correctly (`7318`); full `sync_to_erp_portal()` re-run against that same STO correctly reached the MS SQL stored proc (rejected only as an expected duplicate - that exact STO was already synced in a prior session, confirming the DB/stored-proc path itself is unaffected and correct). Self-tested (small, well-verified backend-only change) - no testing_agent run needed.
- **P0 issue closed** - no Basis/PDI dependency remains for HSN Code.

---


## Session update (2026-08-24, continued 9) - Goods Issue retry + live-stock-check + progress bar visibility, and RTV root-cause VALIDATED (not a hard limitation)

- Real user-reported GI failure: "Determination of source inventory failed for material P27175 - Inventory in logistics area not available" (order STO-000020, real SAP order 30116).
- User's explicit asks: (1) a Goods Issue retry should live-fetch that item's real SAP stock at the exact source warehouse first, then push GI again; (2) the confirm dialog's progress bar should visibly show Goods Issue happening, not hide it as an invisible background-only field.
- **Backend (`stock_transfer_service.py`)**: `try_post_goods_issue()` now calls new `_live_source_stock_qty()` (queries `sap_inventory_client.get_inventory_detail(warehouse_ids=[f"{site}/{warehouse}"])`, sums usable rows) BEFORE ever calling `post_goods_issue()`. If live stock < requested qty, sets `gi_status="insufficient_stock"` with a clear message and returns "waiting" (the existing 20-min/20s-interval background loop keeps re-checking automatically, never even hits SAP's GI endpoint while stock is short). New `reset_goods_issue_for_retry()` (validates status=created_in_sap, gi_status in failed/not_found_timeout, and no already-running job via new `gi_job_running` flag) + `mark_gi_job_started()`. `mark_goods_issue_timed_out()` now takes a stock-specific message variant for when the 20-min window expires while still `insufficient_stock` (avoids the misleading generic "SAP hasn't produced the delivery" text). New `StockTransferOrderNotFoundError` (distinct from `StockTransferValidationError`) so 404 vs 400 is correct.
- **Backend (`server.py`)**: new `GET /stock-transfer/orders/{sto_id}` (single-order lookup, backs the dialog's live GI polling) and `POST /stock-transfer/orders/{sto_id}/retry-goods-issue` (resets then restarts `_run_goods_issue_job`, same pattern as the automatic post-creation GI loop).
- **Frontend (`StockTransferPage.js`)**: `STO_STEPS` now has a 4th step "Post Goods Issue (SAP delivery)". After "Create in SAP" finishes, `pollGiStatus()` polls the new single-order endpoint every 4s and renders a live status box (`stock-transfer-dialog-gi-status`) in the SAME confirm dialog - updates through waiting/insufficient_stock/posted/failed without auto-closing; user can close any time, the background job keeps running regardless (`giPollStopRef` stops the frontend poll on close, not the backend job). Detail modal's GI status box gained an `insufficient_stock` branch and a "Retry Goods Issue" button (shown only for failed/not_found_timeout) calling the new endpoint. Recent Orders table's GI badge gained an "Insufficient Stock" (amber) state.
- **Tested**: `testing_agent` iteration_114 - 100% (17/17 backend pytest incl. 1 real live SAP retry attempt + full state-machine matrix; frontend Playwright incl. a brand new live STO creation SAP order 30145 verifying the 4-step bar + live GI box, posted in ~35s). 3 minor action items (404 vs 400, no idempotency guard, misleading timeout message) all fixed post-test and manually re-verified live (unknown-id -> 404, duplicate retry -> 400 "already running", `gi_job_running` correctly clears to false on terminal states).
- **IMPORTANT root-cause finding, VALIDATED not guessed** (user pushed back on an initial overly-broad claim that RTV-sourced GI "can't be automated" - correctly, since they can do it manually in SAP): live-tested calling the same SAP action with `TaskBasedIndicator=true` on the exact same stuck document -> got a DIFFERENT error ("Logistics model for Standard Shipping with task for site ID P1 (RAY INTERNATIONAL-P1) missing"), and cross-checked against SAP's own official support KB for this error family. Real cause: the `P1-RTV` Logistics Area is very likely missing its "Logistics Use" setting or "Inventory-Managed" checkbox (Supply Chain Design Master Data -> Locations -> Edit Layout) - a master-data gap, not an architectural "RTV can never do a GI" limitation. A human in the SAP UI can manually type in the Logistics Area/Identified Stock when auto-determination fails (which is what "doing it manually" achieves); our automated `PGIInBackground` call has no such override parameter (confirmed via the full WSDL/metadata - only 6 boolean flags, no location override). Told directly to the user: ask Basis to fix `P1-RTV`'s config; automatic GI should then work with zero code changes. STO-000020 is a permanent live fixture for this (never expect it to succeed until Basis fixes the site).

### STO doc schema addition this session
`stock_transfer_orders` also has `gi_job_running: bool` (Aug 27 2026) - true while a GI polling loop (auto or manual retry) is actively in-flight for that order, false otherwise - prevents overlapping retry loops.

---

## Session update (2026-08-24, continued 6) - ManageODIn (deprecated legacy SOAP) dead-ended too

- User found via SAP's "Further Usage of Extension Field (Adaptation Mode)" screen that `VehicleNo`/`PlaceOfSupply` are already checked-available on `ManageODIn`'s **Update** operation (message `ODUpdateRequest_sync`, namespace `http://sap.com/xi/AP/CustomerExtension/BYD/A4CF6`) - a real, purpose-built SOAP write action, distinct from the raw OData PATCH already proven blocked post-release. User downloaded and shared the real enhanced WSDL (`/tmp/manageodin.wsdl`).
- Parsed it: confirmed endpoint `manageodin`, SOAPAction `http://sap.com/xi/A1S/Global/ManageODIn/UpdateRequest`/`.../ReadRequest`, and the exact extension element names - only `VehicleNo` and `PlaceOfSupply` are exposed on this service so far (not TransportationMode/GRNo1/DateOfSupply - those 3 still need the same "Add Field" step if this path were to pan out).
- Built and fired a well-formed SOAP request (mirrors this codebase's existing A1S/Global convention, e.g. `sap_production_lot_client.py`) against BOTH Update and a plain Read on our real test delivery (order 30098's UUID) - **both failed identically**: generic `"Web service processing error; more details in the web service error log on provider side"`, with both `itadmin` and `_EMERGENTBOM` credentials. This isn't an auth or XML-shape issue (same generic failure on a basic Read, which needs no custom fields at all) - matches SAP's own deprecation note: `ManageOutboundDeliveryIn` requires its process component to be "scoped" in Business Configuration ("Production and Site Logistics Execution" deployment unit), which this tenant likely hasn't done for this specific (deprecated) service. That's a further Basis config step, and an uncertain/risky one given it's flagged deprecated by SAP in favor of `II_MANAGE_OUTBOUND_DELIVERY_IN`.
- **Recommendation given to user**: don't chase this deprecated-service scoping rabbit hole further - stick with the already-relayed Option A ask (expose the 5 fields on `CustomerRequirement` + `ManageCustomerRequirementIn` SOAP service, a document we fully control at creation time, zero race condition). Still pending Basis action.

## Session update (2026-08-24, continued 7) - GST fields: SOLVED, live, zero Basis dependency

- User pushed back that these are "just 4 basic custom fields" and asked for any workaround "through the system directly". Investigated Basis's Adaptation-Mode-only options (Extension Scenarios tab on the original "Vehicle No." field) - confirmed NO scenario exists connecting Customer Requirement to Outbound Delivery/Request (only ODR<->OD and neighboring docs) - creating a new one needs a Cloud Applications Studio Process Extension Scenario (real SDK work), so that path stays closed.
- **Found the real, working, zero-Basis-dependency fix**: `ManageCustomerRequirementIn`'s own WSDL (`/tmp/mcr.wsdl`) already supports a standard `TextCollection` (Note) on the `CustomerRequirement` header - a field we already send data to today, no custom field/extensibility involved at all. Live-tested: TypeCode "10011" ("Internal comment") is the valid SAP GDT code for this BO.
- **Implemented + verified fully live** (real order 30143/STO-000024): `sap_sto_client.py`'s `check()`/`maintain()` accept an optional `note_text`. STO doc now has `gst_note_pushed: bool`. Frontend "GST Push" badge simplified to "GST Recorded" (green).
- Removed the old, confirmed-non-working `sap_outbound_delivery_client.push_gst_fields()` (OData MERGE on Outbound Delivery Request/Delivery, always rejected read-only).
- Live end-to-end tested. Feature complete, no Basis/PDI dependency remains for GST fields - **P0 issue closed**.

- **Moving Average cost fix**: shipped, verified live.
- **Live SAP write for Stock Transfer Orders** - DONE:
  - `sap_sto_client.py`: raw-XML SOAP client for `ManageCustomerRequirementIn` (Check-then-Maintain, PartialDeliveryControlCode=9, DeliveryPriorityCode=1). `ShipToLocationID` must equal the Ship-to SITE ID, not a warehouse code.
  - `stock_transfer_service.submit_order_to_sap()` + `server.py`'s `/api/stock-transfer/orders` (background job, `sap_job_id`) + `/api/stock-transfer/orders/sap-status/{job_id}`.
  - Frontend: 2x-safety confirm dialog with real step-by-step progress bar.
  - **Verified live on real production SAP**: orders 30007, 30095, 30111, 30113, 30114, 30116, 30143, 30145 all created successfully via the app.
- **Full Goods Issue automation** - DONE:
  - `sap_outbound_delivery_client.py`: custom OData service `odataoutboundemergent`, auth via ByD Business User (`SAP_USERNAME`/`SAP_PASSWORD`="itadmin").
  - Safe link: `OutboundDeliveryRequestItemBusinessTransactionDocumentReferenceSalesOrderCollect` filtered by our Customer Requirement's UUID, `TypeCode`="814".
  - `PGIInBackground` (TaskBasedIndicator=false, AutoReleaseOutboundDelivery=true) posts GI + releases delivery in ONE call.
  - Background loop polls every 20s for up to 20 min. As of Aug 27 2026 (this session), always live-checks source-warehouse stock first (see top entry) and supports a manual "Retry Goods Issue" button.
- **GST / E-way bill compliance fields** - DONE: 5 mandatory fields, pushed via the Customer Requirement Note (see above), captured/validated/stored on every STO doc.
- **Table visibility improvements**: Recent Orders table shows "SAP Order ID"/"Goods Issue" columns (now incl. "Insufficient Stock" amber state). Detail modal's GI status box independently colored, human-readable error via `parseGiError()`.
- Tested: `iteration_111.json` (STO live write + progress bar), `iteration_112.json` (GST fields + GI automation), `iteration_113.json` (admin missing-planning-data notification + retry flow), `iteration_114.json` (GI retry + live-stock-check + 4-step progress bar) - all passed.

### Outstanding Basis/PDI asks
1. **P1-RTV Logistics Area config** (NEW, Aug 27 2026): needs "Logistics Use" + "Inventory-Managed" checked in Supply Chain Design Master Data -> Locations -> Edit Layout, to unblock automatic Goods Issue for STO-000020-class orders sourced from Return-to-Vendor warehouses. Validated root cause, not yet fixed (Basis-side action).
2. Moving Average / STO live write / Goods Issue / GST fields: all resolved, no outstanding Basis dependency.
3. SAP Gross Weight Field (P3): still blocked, SAP Basis team to expose custom field on `materialgeneralinfo`.

### STO doc schema (cumulative)
`stock_transfer_orders`: `sap_order_id`, `sap_order_uuid`, `gi_status` (null|"awaiting_delivery"|"insufficient_stock"|"posted"|"failed"|"not_found_timeout"), `gi_error`, `gi_job_running` (bool), `outbound_delivery_object_id`, `transportation_mode`, `vehicle_no`, `place_of_supply`, `gr_no`, `date_of_supply`, `gst_note_pushed`, `error_message`.

### New env vars (backend/.env)
`SAP_SOAP_STO_ENDPOINT`, `BYD_ODATA_BASE`, `BYD_ODATA_VHOST` (reuses `SAP_USERNAME`/`SAP_PASSWORD`="itadmin").

---

## Earlier history (BOM Explorer, Inventory, Production Confirmation, Store Approval, Access Management, Entra SSO rollout, Purchasing Plan, MRP, etc.)
See git history / prior PRD versions for the full session-by-session log predating 2026-08-24 continued 6 (STO/GI/GST work) - condensed here to keep this file manageable. Key standing features, all live and tested: Microsoft Entra ID SSO app-wide (roles: super_admin/admin/user, `bound_sites` for Store Approval site restriction), Production Confirmation (order creation, component stock check, SFG-only live refresh, Stop/cancel job handling), Store Approval (RM+QC live refresh, site-scoped), BOM Explorer + BOM Stock modal (search/filter/collapse/xlsx export), Purchasing Plan, L1/L2 Report, Quota Allocation, Supplier Master, Access Management, SAP connection status indicator on every page, Moving-Average-based costing everywhere (not Standard Cost).
