# PRD: SAP ByDesign Ops Portal (VMS/STO/GRN/Supplier Portal)

## Original problem statement
React + FastAPI + MongoDB app integrated with SAP Business ByDesign (OData + SOAP).
Covers Supplier Portal, GRN, Production Confirmations, Stock Transfer Orders (STO).

## Architecture
- `/app/backend/stock_transfer_service.py`: STO orchestration (create, submit to SAP, Goods
  Issue, outbound pre-relocation to `{SITE}-HOLD`).
- `/app/backend/inbound_receipt_service.py`: Inbound Receive flow + post-receipt relocation
  from `{SITE}-HOLD` to the real target warehouse.
- `/app/backend/supplier_shipment_service.py`: Supplier portal GRN logic.
- `/app/frontend/src/pages/supplier-portal/`: Supplier-facing UIs.
- `/app/frontend/src/pages/GrnApprovalPage.jsx`: Internal admin GRN view.

## STO "-HOLD" staging warehouse workaround (both directions)
SAP's Material Flow Destination/Basic Rule forces every STO (in and out) at a site through
that site's own `{SITE}-HOLD` staging warehouse. Every site now has its own `-HOLD`
warehouse (P1-HOLD, P2-HOLD, P3-HOLD, P8-HOLD, ...) - confirmed live by the user (Sep 2026).
- Outbound: `stock_transfer_service._relocate_items_to_source_hold_warehouse` moves stock
  from the real source warehouse into `{ship_from_site}-HOLD` BEFORE the STO is created.
- Inbound: `inbound_receipt_service._relocate_receipt_from_hold` moves stock from
  `{ship_to_site}-HOLD` to the real target warehouse AFTER the Goods Receipt posts.
- Both are now fully generalized for ANY site (fixed Sep 2026 session, this fork) - no more
  hardcoded `== "P8"` checks anywhere in either file.

## Goods Issue architecture (Sep 18 2026 decision - DO NOT revert)
PGIInBackground automatic single-line Goods Issue was tried and abandoned after 7
documented live failures (see `sap_outbound_delivery_client.py` docstrings, incl. silently
wrong postings). Current architecture: ALL STOs (single or multi-line) require a manual
Goods Issue completion in SAP UI, with an automatic Release attempt + Analytics-based
auto-detection (`try_post_goods_issue`) as a background poll, and a "Complete STO Process"
manual-confirm button (`check_manual_gi_completion`) as fallback. User explicitly confirmed
(this session) to leave this as-is - do not restore old single-line automation.

## STO Inbound Goods Receipt - 4-route API automation re-investigation (Sep 21 2026, this session)
User mandated re-checking 4 precise API routes (no Playwright/ABSL/manual UI) for fully
automating STO Goods Receipt. Full report: `/app/memory/STO_INBOUND_4_ROUTE_FINAL_REPORT.md`.
- **Route 1 (PGRBackground)**: DEAD - re-confirmed, SAP disables the action tenant-wide (KBA
  3583076/2691388), even with the correct Logistics Model present.
- **Route 2 (Task-Based Receiving)**: DEAD for STO - re-verified LIVE today at P8: of 128 real
  Site Logistics Tasks, only 3 are Put Away (`OperationTypeCode=11`) and all 3 belong to a
  vendor-PO-GRN test (PO 29346), zero reference any STO. Task-based receiving only triggers for
  notifications WE create via SOAP (vendor GRN flow), never for SAP's own auto-created STO
  delivery notification.
- **Route 3 (API-triggered Warehouse Request Run)**: DEAD - no standard OData/SOAP API exists
  for this; only path is custom ABSL (forbidden).
- **Route 4 (3PL / Externally Managed Receiving)**: genuinely new, untested avenue found
  (`RequestInboundDeliveryExecution`/`ProcessInboundDeliveryExecutionConfirmation` B2B SOAP
  messages) - but investigated the IMPACT with the user and it was explicitly DROPPED: marking
  a site "Externally Managed" disables ALL standard internal warehouse tasks for that ENTIRE
  site (not just STOs) - would break the already-working vendor-GRN Put Away automation (EM1)
  too, plus break standard stock adjustments/scrapping at that site, and needs new master data
  (Warehouse Provider Business Partner, transport lanes). User's decision: "Not worth the
  disruption."
- **User's final decision**: no PURE-API route exists/is viable - the EXISTING Playwright-driven
  "Receive" button (`InboundReceiptsPage.js` -> `sap_playwright_pgr_service.py`, see correction
  below) remains the permanent mechanism. The already-drafted SAP Support ticket for Route 1
  (`SAP_SUPPORT_TICKET_DRAFT_InboundPGR.md`) - user has NOT yet confirmed whether to submit it.

### CORRECTION (Sep 21 2026, this fork - user caught a misleading report)
The phrase "accept manual STO receiving in SAP UI as permanent" above (and the identical phrase
at the older "STO Inbound Receiving via API - DEFINITIVELY DEAD" entry below) is MISLEADING and
was corrected after the user flagged it. Receiving is **NOT actually manual for the user** - the
"Receive" button on `InboundReceiptsPage.js` (Pending tab) already fully automates it end-to-end:
one click -> headless Playwright (`sap_playwright_pgr_service.post_goods_receipts_via_ui`) logs
into real SAP UI, opens the delivery, clicks "Post Goods Receipt", applies any qty overrides,
verifies success, then auto-relocates stock out of `{SITE}-HOLD` into the real target warehouse.
Confirmed LIVE and working via DB check this session: 31 STOs successfully received this way
(various sites), 9 failed (retriable via the Retry button), 0 stuck pending. What's actually true
is narrower: there is no *pure API/OData/SOAP* way to do this (all 4 routes above are dead) - the
UI-automation (Playwright) path was never removed and remains the real, current, working
mechanism. Do not describe this feature as "manual" again - it is one click in the app.

## Major speedup - vendor-scoped Pull via OData analytics report (Sep 20 2026, same session)
User's ask: "is there a way we can use an OData report to pull this faster" - investigated and
found YES. `sap_po_analytics_client`'s existing report (`RPSRMPO_B02_Q0004QueryResults`, already
used for Open Qty) has a REAL, working server-side vendor filter - live-verified
`$filter=CSELLER eq 'H1330'` returns ONLY that vendor's rows in ~3s. This is a completely
different SAP service than `sap_po_client.py`'s SOAP one (whose vendor filter is silently
ignored by this tenant - that's WHY the whole ID-range-scan architecture existed in the first
place). This report also exposes item description/product/qty/price/site/lifecycle-status - a
full 1:1 field match for the existing cache row schema.
- New `SAPPOAnalyticsClient.fetch_pos_for_vendor(vendor_code)` - single filtered OData GET,
  excludes the same "not open" states as the SOAP path (`CITM_LFCYCLE_ST` in
  {1,4,8,10} = In Preparation/Rejected/Cancelled/Finished - verified these are the SAME lifecycle
  codes `sap_po_client.LIFECYCLE_STATUS_TEXT` already uses).
- Both "Pull Latest POs" buttons now call this directly and SYNCHRONOUSLY (no more job_store/
  polling/cooldown machinery - that existed purely to make the old 60-100s+ whole-tenant scan
  tolerable; this is fast enough to just await). Removed `_trigger_manual_po_refresh`/
  `_run_manual_po_refresh`/`PO_MANUAL_REFRESH_COLLECTION` entirely.
- Admin endpoint is now `POST /admin/act-as-supplier/{account_id}/purchase-orders/refresh`
  (was tenant-wide before - now properly scoped to that account's own vendor_code, matching the
  sibling GET purchase-orders endpoint's pattern).
- Verified live end-to-end: H1330 pull took ~14s (202 open cache rows correctly refreshed,
  matches SAP), RAD-P2-S admin pull took ~21s (30 distinct POs) - both a large improvement over
  the previous 60-100s+/1-3min whole-tenant scan, and now genuinely vendor-scoped (cost no longer
  tied to total tenant PO volume). The 10-min delta loop, 4-hour full-refresh loop, and PO 29735
  safety-margin fix (all still SOAP-based, all still tenant-wide) are UNCHANGED - this only
  replaced the two manual "Pull" button code paths.

## Feature added - 4-hour full cache + fast/lightweight Pull (Sep 20 2026, same session)
User's ask: build a 4-hour background full cache of ALL supplier POs (dashboard/act-as-supplier
load fast either way, from cache), and make "Pull" only fetch new-or-changed POs, not redo
everything.
- New `sap_po_client.fetch_full_window()` - comprehensive sweep of the last
  `FULL_REFRESH_LOOKBACK_IDS` (8000) PO IDs (vs the existing `fetch_recent_window`'s ID-delta-only
  approach, which can find NEW POs fast but can never see a CHANGE - qty/price/status edit - to
  an already-cached one, since it only looks at IDs above the watermark). Independent of
  `sap_po_watermark` so it never perturbs the fast loop's own state.
- New background loop `start_supplier_po_full_refresh_loop` (`server.py`) - runs
  `fetch_full_window` + `refresh_all_vendor_caches` every 4 hours, catching both new POs and
  edits to existing ones as a comprehensive safety net alongside the fast 10-min loop.
- Decision: kept BOTH "Pull Latest POs" buttons (supplier dashboard + admin Act-as-Supplier) on
  the FAST `fetch_recent_window` path (~75s-3min observed), not the new heavy `fetch_full_window`
  (~5+ min observed under SAP load) - a live test of wiring Pull to the full window made the
  button noticeably slower, contradicting the original "speed things up" ask. Catching "changes
  to existing POs" is handled by the new 4-hour background job instead, not the on-demand button.
- Also fixed a latent frontend bug while here: if a pull job is still "running" when the 40x5s
  polling loop gives up, both buttons now correctly toast "still pulling, check back" instead of
  wrongly claiming success.

## Bug fix - PO missing "even after pulling" (Sep 20 2026, same-day follow-up)
Real user report: PO 29735 didn't show up even after clicking "Pull Latest POs". User confirmed
29735 IS the tenant's actual highest real PO. Root cause: `_has_po_id_greater_than` (used by the
binary-search max-ID discovery) checks raw SAP ID existence with NO status/lifecycle filtering,
unlike the real fetch (`_parse_pos`, which filters out cancelled/not-yet-released/etc). Some OTHER
document sharing this ID range (not a real usable PO) had already pushed the stored watermark up
to 29802 in an earlier cycle, at a point when PO 29735 wasn't released yet - once it later WAS
released, every future `(lower_bound, current_max]` scan sat permanently above it (lower_bound
already at 29802), so it could never be found again by a normal cycle.
- Fix (in `sap_po_client.fetch_recent_window` itself, not just the manual-pull path - so BOTH the
  automatic 10-min loop and every manual pull self-heal from this class of gap going forward):
  every cycle now re-sweeps a 450-ID safety margin below the stored watermark
  (`RECENT_WINDOW_SAFETY_MARGIN_IDS`) in addition to the normal range. Cheap (~1 extra chunk) and
  safe (goes through the same real, filtered fetch path - a phantom ID won't come back either).
  The function's returned `lower_bound` (used for cache-expiry decisions) is unchanged, so this
  can't cause any cached PO to be wrongly expired.
- Verified live end-to-end: manual pull completed (`vendors_updated: 63, total_line_items: 571`,
  no errors) with the refactored code; PO 29735 (5 line items, vendor RAD-P2-S) already confirmed
  visible via the Act-as-Supplier PO API from the earlier (pre-refactor) fix attempt.

## Feature added - manual "Pull Latest POs" (Sep 20 2026, same session)
User's ask ("add option to pull") - on-demand refresh so a supplier doesn't have to wait out the
~10 min background PO cache cycle explained above.
- `POST /api/supplier-portal/purchase-orders/refresh` - kicks off the SAME `fetch_recent_window` +
  `refresh_all_vendor_caches` the background loop uses, as a `job_store`-tracked background task
  (returns instantly, never blocks the request - a full fetch is a global all-vendor SAP scan,
  60-100s+). A 45s cooldown (`po_manual_refresh_state` singleton doc) makes repeat clicks reuse
  the same in-flight/just-finished job instead of queuing redundant global SAP scans.
- `GET /api/supplier-portal/purchase-orders/refresh/{job_id}` - status poll.
- Frontend: "Pull Latest POs" button on `SupplierDashboardPage.jsx` (next to the Open/All filter
  toggle) - polls every 5s, shows a spinner while running, toasts on success/failure, then
- Also added to the ADMIN "Act as Supplier" page (`ActAsSupplierPage.jsx`) - same button next to
  the PO search box, shares the exact same backend job/cooldown via
  `POST/GET /api/admin/act-as-supplier/purchase-orders/refresh[/{job_id}]` (both endpoints now
  call a shared `_trigger_manual_po_refresh()` helper). Verified live with the `act_as_supplier`
  permission (positive/negative synthetic test sessions from iteration_183) and via screenshot.

## Root cause fix - stray "Moved to P8-RM" note (Sep 20 2026, same-day follow-up)
Real user report: Confirmed GRNs list started showing a "Moved to P8-RM" badge again on
S000020/S000021 after the Put Away Task fix above, even though "no warehouse movement" was the
explicit design. Root cause: the Warehouse dropdown UI was removed earlier, but its underlying
`useEffect` (auto-fetch `/admin/grn/warehouses/{siteId}` + auto-default to the site's RM
warehouse) was left running - `warehouseId` state kept getting silently populated (e.g. "P8-RM")
and sent on the approve payload. That non-empty `warehouse_id` re-armed
`manually_confirm_inbound_delivery`'s own goods-movement gate (called from the restored
background finisher's Delivery ID auto-fetch step) - it found stock "already in P8-RM" (a
harmless no-op) but still set `sap_movement_status: "posted"` + a `per_item` note, which the
Confirmed GRNs list renders as "Moved to {warehouse}".
- Fix: removed the entire warehouse auto-fetch `useEffect` + `warehouses`/`warehousesLoading`
  state from `GrnApprovalPage.jsx` - `warehouseId` is now a permanent `""` constant. With it
  always empty, `manually_confirm_inbound_delivery`'s movement gate stays permanently false, so
  no future GRN can show this badge again. S000020/S000021's existing "Moved to P8-RM" badges are
  harmless historical data (no real move happened, item was already in place) - left as-is.

## CRITICAL FIX - Put Away Task confirmation restored (Sep 20 2026, same session)
Real production bug found via user's own SAP UI screenshot: shipment S000016/PO 29724 was
marked "posted" by this app, but SAP's real Inbound Warehouse Request stayed "Released" with
Fulfilled Quantity 0 for all 4 lines - the Goods Receipt never actually completed in SAP. Root
cause: `_auto_finish_full_auto_grn` (Put Away Task confirmation, the SAP action that finalizes
ANY Goods Receipt in this EM1 logistics model) was wrongly removed together with the separate
"goods movement into a target warehouse" step when `skip_movement=True` was introduced - these
are two DIFFERENT things, only the latter was meant to be skipped.
- Fix: `_start_full_auto_grn_job` now re-schedules `_auto_finish_full_auto_grn` after a successful
  post (only Put Away confirm + existing "Fetch from SAP" Inbound Delivery ID auto-fetch - the
  Goods Movement retry block was permanently DELETED from that function, not just skipped).
- `manually_confirm_inbound_delivery`'s own goods-movement block only fires if `warehouse_id` is
  set on the shipment - it's now always empty string for full-auto GRNs (Warehouse dropdown
  removed from UI), so no extra flag was needed to keep it a no-op.
- Retroactively fixed S000016 live in SAP: confirmed its real Put Away Task (69222, PO 29724) -
  Fulfilled Quantity now matches Planned Quantity, Delivery ID 53685 recorded. Verified only
  S000016 was affected (every full-auto GRN before/after this incident window already had
  `put_away_confirmed: True`).

- Confirmed (Sep 20 2026, same-day follow-up): the S000021/Delivery 53751 "Fulfilled Quantity: 0"
  screenshot was SAP propagation lag, not a broken fix - re-queried the confirmation report
  minutes later and CONF_QUAN/INV_QUAN now show the real confirmed quantities matching planned
  qty for all 4 products. The Put Away Task confirmation fix above is genuinely working end-to-end.

## Completed this session (Sep 20 2026 fork continuation)
- Fixed GRN Approval frontend bug: form validation still required `warehouseId`
  after the Warehouse dropdown was removed from the UI - blocked ALL GRN
  submissions ("Select a Site and Warehouse before approving"). Removed the
  `warehouseId` check from both `approve()` and `openApprovalConfirm()` in
  `GrnApprovalPage.jsx` - only Site is required now. Verified live: confirm
  dialog now opens correctly with just Site set.
- Verified (user asked to confirm): Site ID on GRN IS derived from the
  shipment's own PO data (`derive_ship_to_site_id`, exact `ship_to_site_id`
  per PO line in `supplier_portal_po_cache`), NOT from entity-level locking -
  entity-wide list (`allowed_site_ids_for_buyer_code`) is only a fallback for
  legacy shipments where the exact site isn't cached. Confirmed via live DB
  query on 2 real shipments.
- Removed the "Warehouse movement skipped - Goods Receipt has not posted to
  SAP yet" banner + its Retry button from the GRN lookup detail view -
  `sap_movement_status` is now PERMANENTLY "not_applicable" for every GRN
  (skip_movement=True is always on), so this banner was always stale/
  confusing. Now hidden whenever `sap_movement_status === "not_applicable"`
  (same pattern the Confirmed GRN dialog already used).

## Completed earlier this session (Sep 2026 fork)
- Fixed blocking lint error in `stock_transfer_service.py` (stale `RELOCATION_SITE_ID` /
  `_relocate_items_to_p8_source_warehouse` refs from an incomplete prior refactor).
- Generalized OUTBOUND pre-STO relocation to run for any `ship_from_site_id`, not just P8
  (was already partially done in the helper functions; fixed the call site).
- Generalized INBOUND post-receipt relocation (`inbound_receipt_service.py`) to run for any
  `ship_to_site_id`, not just P8 - this was the actual root cause of the user-reported
  "P3->P2 STO background movement not completing" (ship_to=P2 never matched the old
  P8-only check, so stock stayed parked in P2-HOLD after receipt).
- Updated/fixed stale tests in `tests/test_p8_receipt_relocation_iter181.py` that asserted
  the old P8-only rejection behavior.
- Verified via pyflakes (clean), pytest (17/20 pass; 2 failures were transient 502 gateway
  blips confirmed by retry, 1 is a pre-existing stale test from an earlier unrelated commit
  f0b2b06, not caused by this fix), and a direct mocked-call-path check across P1/P2/P3/P8.
- Fixed real GRN Approval bug (`GrnApprovalPage.jsx`): shipment code input was hard-capped
  at `maxLength={6}` and the instruction text said "6-character", but codes were changed to
  the 7-character `S000001`+ sequential format weeks ago - so any real code of that format
  silently got truncated and always failed lookup. Fixed maxLength (10) + stale text +
  placeholder. Verified live via screenshot: "S000003" now types in full and looks up
  correctly.

## STO Outbound automation via Site Logistics Task - INVESTIGATED THEN REVERTED (Sep 18)
Full investigation, live breakthrough, then a full user-directed revert, all same session -
see `/app/memory/SITE_LOGISTICS_TASK_BREAKTHROUGH_2026-09-18.md` for the complete technical
record (kept for future reference only, NOT the current architecture).
- Proved live: confirming a Site Logistics Pick Task via SOAP (`ManageSiteLogisticsTaskIn.
  MaintainBundle_V1`, real WSDL schema fixes needed - see that file) fully auto-releases the
  Outbound Delivery and posts Goods Issue, IF a Pick Task already exists (only possible on
  site P8, the only site with the EM2 "Standard Shipping with pick lists" Logistics Model).
- Exhaustively confirmed (2 different SAP mass-runs tested live, both either scoped to
  Customer Sales Orders only or selecting 0 for STOs): there is NO way to auto-CREATE that
  Pick Task without a manual "Create Warehouse Task" click in SAP UI - matches 9+ prior
  documented dead attempts. So this path never actually saved any manual effort vs. the
  original single-click flow.
- User's final decision (Sep 18 2026): REVERT ALL OF IT. Deactivating EM2. Back to the
  original, only-ever-accepted flow: create STO in app -> manually click "Create Outbound
  Delivery" + Release (single click) in SAP UI. `sap_site_logistics_client.py` restored to
  its pre-session state; the "extended GI retry" fix in `stock_transfer_service.py`/
  `server.py` fully undone. DO NOT re-attempt this direction unless the user explicitly
  reopens it (e.g. by reactivating EM2 themselves).
- Test STO-000113 (SAP order 32290) created live during this investigation - removed from
  our app DB; still exists in real SAP, user informed, needs manual cancellation by them in
  SAP UI (no safe/proven Cancel API action exists for this document type).

## STO Inbound Receiving via API - DEFINITIVELY DEAD, do not re-investigate (Sep 18)
**PARTIALLY SUPERSEDED Sep 22 2026 - see `/app/memory/CHANGELOG.md` "Part 5" for the full story.**
This conclusion is still correct for the DIRECT-PGR route (`InboundDeliveryPGRBackground`,
permanently disabled tenant-wide per KBA 3583076, unchanged). It is WRONG as a blanket statement
for task-supported sites (confirmed: P1): a different route through the Warehouse Order/Operation
Activity chain (`ConfirmAsPlanned` via a new custom OData service `khinbounddeliveryexecution`) IS
live and working, fully automated (Acknowledge+Release+SAP Event Notification webhook+
ConfirmAsPlanned), with real inventory confirmed posting via `SAPInventoryClient` multiple times.
The "Receive" button on `InboundReceiptsPage.js` now uses this automatically wherever applicable -
see `inbound_receipt_service.start_automated_receipt`/`complete_automated_receipt_for_lot`.
Re-investigated same day whether the outbound Pick-task SOAP breakthrough above also solves
Issue 2 below (Playwright replacement for STO receiving). Answer: NO, confirmed dead twice:
1. Already tested once before (Aug 28 2026, see `sap_playwright_pgr_service.py` docstring).
2. Re-confirmed live today: the user's actual receiving process is one "Post Goods Receipt
   As Planned" UI click - no task/warehouse-request involved at all in practice.
3. The API equivalent of that exact button (`InboundDeliveryPGRBackground`, see
   `sap_inbound_delivery_client.py`) is permanently disabled by SAP itself for this tenant -
   confirmed via SAP's own official KBA 3583076. Genuine platform limitation, not fixable
   from our side.
Decision: accept manual receiving in SAP UI indefinitely (current reality, matches Playwright
being stopped). A SAP Support ticket has been drafted (see
`/app/memory/SAP_SUPPORT_TICKET_DRAFT_InboundPGR.md`) for the user to submit - the only
remaining path that could unblock this, but depends entirely on SAP's response.

## BREAKTHROUGH this session - see /app/memory/SOAP_GRN_BREAKTHROUGH_2026-09-18.md
Direct SOAP GRN posting proven live (no Playwright) via `sap_inbound_delivery_notification_client.py`
- single-line, multi-line, and partial-quantity all confirmed working on real POs, once a
site's "Standard Receiving (with task)" Logistics Model is created+released (done for P8
as "EM1"; P1/P3/others still need it). Full details, exact schema, and next steps are in
that file - READ IT before touching this area again.

Confirmed NOT an issue: STO creation/receiving is unaffected by the Logistics Model gap
(51 multi-line STOs already succeed today; Playwright-based receiving already works fine
on P1 despite its missing "with task" model) - see same file's dedicated section.

## In progress this session (see /app/memory/SESSION_NOTES_2026-09-18.md for full detail)
- ERP sync resume: DONE. `STO_ERP_SYNC_PAUSED` flipped to "false" in `/app/backend/.env`
  (Sep 17-18 2026), backend restarted and verified healthy. New STOs will now sync to the
  legacy ERP portal again; existing STOs stuck at `erp_portal_status: "paused"` were
  deliberately left untouched per user's instruction (no retries). User confirmed no
  ERP-side field/format changes needed accounting for.
- GRN S000001 warehouse-move bug (site P3, items IRON-SCR/13INTIEBELT): same P8-only
  `{site}-RM` hardcoding bug found in a 3rd file, `supplier_shipment_service.py`
  (`_post_goods_movement_for_items`). FIXED (generalized `source_area` to `f"{site_id}-HOLD"`
  for all sites, mirrors Task 1 STO fix) and self-verified via mock. ALSO fixed 2 related
  frontend bugs in `GrnApprovalPage.jsx`: (1) Confirmed GRNs detail modal was missing the
  warehouse-move Retry button entirely (only existed in the Pending Shipments modal) - added
  `retryConfirmedMovement` + a "Warehouse Move" status/retry section; (2) Diagnostics modal
  always showed "item(s) below were dropped...likely crashed immediately" for ANY "posted"
  PO regardless of whether anything was actually wrong - now correctly distinguishes clean
  success / dropped items / warehouse-movement-failed-only (via new `poDiagnostics()` helper
  that attaches `sap_movement_result` errors matched by po_number), and the PO Status column
  label now shows "Posted (movement failed)" when relevant. Verified live via screenshot
  against real GRN S000001 data - both button and new diagnostics messaging render correctly.
  STILL AWAITING: user to click the (now working) Retry button live on GRN S000001 and report
  whether IRON-SCR/13INTIEBELT actually post successfully now.

## Known pre-existing issue (not introduced this session, not fixed)
- `tests/test_p8_receipt_relocation_iter181.py::test_relocate_returns_stock_not_exist_error_for_zero_hold_stock`
  fails because commit `f0b2b06` (STO-000100 incident fix) removed the local inventory-cache
  pre-check that this test depends on, in favor of trusting SAP's live rejection directly.
  Test asserts SAP is never called for a nonexistent product; that's no longer true by
  design. Needs updating in a future session (not blocking, not in original scope).

## INCIDENT: PO 25271 items 2 & 5 cancelled while a supplier delivery was in flight (Sep 21 2026, investigation)
User reported: unable to receive a GRN (Supplier Delivery Notification BI/2026-27/110-
S000032-25271, sender Balaji International) in SAP. Root cause traced:
- Someone using our app's Open Purchase Orders page (`OpenPurchaseOrdersPage.jsx`) clicked
  "Cancel Item" on PO 25271's item 2 (and separately item 5) on 19.09.2026 - this IS a real,
  working feature (built Sep 14 2026). SAP's Change History always attributes API writes to
  our shared technical user "_EMERGENTBOM", never the actual human - so it looked like "the
  system did it," but it was a genuine human click through our UI. **We have NO audit trail
  of who clicked it** - this is the gap fixed below.
- **New finding, contradicts the Sep 14 2026 docstring**: that docstring claimed SAP always
  rejects item-level cancel with "Deleting data not possible; deletion disabled" (based on a
  disposable test PO). Real PO 25271 proves this is NOT universal - SAP accepted it and
  performed a soft-cancel (ItemStatusCode -> Canceled, tax zeroed, CancellationStatusCode ->
  4) instead of rejecting. The real behavior depends on the item's own state (e.g. whether a
  Follow-Up Document already exists), not a fixed tenant-wide lockout. Docstrings updated to
  reflect this + a strong warning about checking for in-flight supplier deliveries first.
- Same day, the supplier submitted their own Delivery Notification (SAP's native supplier
  channel, NOT our custom Supplier Portal - confirmed zero matching records in
  `supplier_portal_shipments`) against that same now-cancelled item. Since the item's open
  quantity dropped to 0, SAP flagged the notification "Consistency Status: Inconsistent" and
  stuck it at "Release Status: Not Released" - permanently blocking GRN posting.
- **Confirmed via SAP's own official support docs**: a cancelled PO item CANNOT be reversed/
  reopened in ByDesign - no "undo cancel" exists. Fix path (not yet actioned - user said "fix
  it later"): either cancel the stuck Delivery Notification and have the supplier resubmit, OR
  add a new replacement PO line item for the same product/qty and redirect the delivery there.
- **Fix applied this session**: `cancel_purchase_order`/`cancel_purchase_order_item` (server.py)
  now log every attempt (who/when/po/item/outcome) to a new `po_cancel_audit_log` Mongo
  collection, using `request.state.user` - so a repeat incident is traceable to the actual
  human, not just SAP's generic technical user. Historical backend logs were checked for the
  Sep 19 request but had already rotated out (uvicorn access logs have no timestamps and this
  app generates high request volume) - could not recover WHO clicked it this time.
- **Follow-up fix (same session, user's explicit ask)**: Cancel PO/Cancel Item now requires its
  OWN dedicated `po_cancel` permission (added to `PAGE_CATALOG`), separate from
  `open_purchase_orders` (view-only, previously the ONLY gate on this page) or `purchase_order`
  (PO creation, the general catch-all these 2 endpoints used to silently fall under with no
  distinct check at all). Since the cancel routes have the PO number/item ID in the MIDDLE of
  the path (`/api/purchase-orders/{po}/cancel`), plain prefix matching in `PAGE_ROUTE_RULES`
  couldn't isolate them - added a small regex-based special case
  (`_PO_CANCEL_PATH_RE`) checked BEFORE the prefix list in `auth_service.resolve_required_pages`.
  Frontend (`OpenPurchaseOrdersPage.jsx`) now hides both Cancel buttons behind
  `hasPageAccess("po_cancel")`. Also corrected the Cancel confirmation dialog's misleading text
  (it claimed SAP always rejects cancelling an item with a Follow-Up Document - PO 25271 proved
  that false). Live-verified via curl with 3 synthetic test users: a `po_cancel`-only user
  passes this gate but is still 403'd on unrelated `/api/purchase-orders/*` endpoints (no
  over-grant); a no-permission user is 403'd; audit log correctly records the acting user.
- **Not yet done** (user deferred): the actual PO 25271 fix (new line item or cancel+resubmit
  Delivery Notification).

## Proactive "Valuation not active" detection - STO creation + GRN (Sep 21 2026, this session)
User's ask: 2 distinct root causes ("site/Planning not active" vs "Valuation not active") can
block STO creation and GRN receiving - want them told apart with a clear message + Retry, not
generic SAP jargon.
- New `SAPValuationClient.has_valuation_level(product_uuids, site_id)` - returns `{uuid: bool}`
  for whether a genuine Valuation LEVEL exists at that site's own PermanentEstablishmentUUID
  (does NOT fall back to another site's level, unlike `get_standard_costs`) - or `None` if
  `site_id` isn't in `SITE_TO_PERMANENT_ESTABLISHMENT_UUID` yet (fail-open, never false-block).
- New shared `sap_material_valuation_data_client.friendly_valuation_error(raw_message,
  site_id)` - rewrites known "Valuation not set up" SAP rejections ("valuation data missing",
  "account det. group is missing", "financials pu") into plain English naming the material +
  site; passes through unrecognized text unchanged.
- **STO creation**: `stock_transfer_service._missing_valuation_products()` now runs BEFORE
  SAP's own Check step in `submit_order_to_sap` - if the destination site has no Valuation for
  any item, marks the STO `sap_failed` with the clear message and upserts an
  `admin_notifications` doc with `type: "missing_valuation"` (new type, alongside the existing
  `missing_planning_data`) - reuses the SAME "Action Needed" panel + Retry button already on
  `StockTransferPage.js`, now type-aware (message + which result field ("valuation" vs
  "planning_logistics") actually gates the Retry button showing/notification resolving).
- **GRN**: `supplier_shipment_service.mark_put_away_confirmed()` now takes a `site_id` param
  and runs the final failure `reason` through `friendly_valuation_error` before building
  `grn_alert_message` - server.py's `_auto_finish_full_auto_grn` already had `site_id` in scope.
- `post_activate_material_site`'s notification-resolve logic is now type-aware too (fetches the
  notification's own `type`, checks `valuation`/`planning_logistics` accordingly) - previously
  ALWAYS checked `planning_logistics`, which would incorrectly resolve a still-broken
  "missing_valuation" notification the instant any activation attempt ran (since Planning was
  never the problem for that type).
- Live-verified the core logic directly against real SAP data: `has_valuation_level` for
  G12NUT correctly returns True@P7/P2, False@P9, None@P6 (no PE mapping); `_missing_valuation_
  products` correctly returns `['G12NUT']` for ship_to_site_id=P9 and `[]` for P7. Did NOT
  create a real STO end-to-end (would need real GST/warehouse fields) - logic-level
  verification only; should be exercised for real the next time an STO actually hits this path.

## BUG FIX: "Set Valuation" lost the helpful "ask SAP admin" guidance for brand-new sites (Sep 21 2026, same session)
User tested G12NUT @ P9 (a site that NEVER had ANY prior Valuation record) via the app -
Planning/Logistics/Availability activated fine, but Valuation showed the BARE SAP error
"Valuation data missing for material G12NUT business residence P9 (RADISH TECHNOLOGIES-P9)"
with none of `activate_site`'s existing friendly guidance ("ask your SAP admin to create it
ONCE via Inventory Valuation work center...", from the Sep 11 2026 6700-302359 @ P9 incident).
- Root cause: `set_valuation()` (added this session) has its OWN try/except around
  `set_account_determination_and_price` that just returned `str(e)` raw - missing the same
  "valuation data missing" enrichment `activate_site()` already has. Since `server.py`
  overrides `result["valuation"]` with `set_valuation()`'s outcome whenever an `amount` is
  given, users lost the helpful message the moment they typed a Cost.
- Fix: added the identical enrichment to `set_valuation()`. Re-verified live (same G12NUT @
  P9 call) - now returns the full guidance text again.
- **This is a genuine, still-unresolved SAP platform limit** (not fixable via API) - P9 needs
  the SAP admin's one-time manual step before this page's Valuation/Cost push will work there.
  Planning/Logistics/Availability activation at P9 is done and fine either way.

## BUG FIX: "Set Valuation" falsely showed SAP's success as an error (Sep 21 2026, this session)
Live-tested via the app: set G12NUT's Cost at P7 to match P8 (5.14 INR) - a real, successful
SAP write (`get_standard_costs` confirmed both sites now read 5.14). But the UI showed it as
FAILED, with the error text literally being SAP's own success confirmation: "Inventory cost
change document 2026-09-00001200 created for company RT (RADISH TECHNOLOGIES)".
- Root cause: `sap_material_valuation_data_client.py::_post` treated ANY non-empty `<Log>
  <Item>` as a fatal error, never checking `<SeverityCode>`. This specific call path
  (pushing a real nonzero Cost onto an ALREADY-Active site) is the first time SAP ever
  returned an INFORMATIONAL Log Item (SeverityCode 1) instead of an empty `<Log/>` - the
  previous only-ever-amount-0 bootstrap path never triggered this.
- Fix: only raise `SAPMaterialValuationDataError` when `<SeverityCode>` is 3-9 (real error) -
  same convention already used by `sap_goods_movement_client.py`/`store_approval_service.py`
  for this exact SAP Log shape. Re-verified live (same call, now returns `{"valuation":"ok"}`).
- `sap_material_create_client.py`'s own `_post` (used by `activate_site`/`set_valuation`'s
  Valuation actionCode call) was NOT touched - confirmed via a live raw-XML test it returns
  an empty `<Log/>` on success, so no equivalent bug there (yet).

## Set Material Valuation (Cost) - merged into Activate Material Site page (Sep 21 2026, this session)
User's ask: set/activate SAP Standard Cost (Valuation) for a material at a Company/Site
directly from the app instead of the manual SAP UI flow (screenshot showed Material 368 with
P4/P6/P7 rows stuck "In Preparation"). First built as a separate section on the SAP Write
admin page, then user asked to move it INTO the existing `/admin/activate-material-site` page
instead (same page, one flow) - final design below.
- Discovered the write capability mostly ALREADY EXISTED (`sap_material_create_client.
  activate_site`/`sap_material_valuation_data_client.set_account_determination_and_price`,
  built Sep 11 2026) - just hardcoded to a 0 opening price and only reachable as an
  error-fallback (fires only when a fresh "In Preparation" site hits "account det. group is
  missing"), never as a direct "set this Cost" action, and never for an ALREADY Active site.
- Generalized `set_account_determination_and_price` to accept a real `amount` (was always 0).
  Added `SAPMaterialCreateClient.set_valuation()` - ONE call that both activates a site's
  Valuation (In Preparation -> Active) if needed AND pushes the given Cost as a new price
  period if already Active (SAP's Moving Average price is period-based history, not an
  in-place edit).
- `ActivateMaterialSiteRequest` now has an optional `amount` field. `POST /admin/material-
  sites/activate` still runs the existing `activate_site` flow unchanged (Planning/Logistics/
  Availability + 0-amount Valuation fallback) when `amount` is omitted (fully backward
  compatible) - but when the caller passes an `amount`, it ALSO calls `set_valuation()`
  afterward and overrides the `valuation` key in the response with that real outcome, so the
  UI reflects the actual Cost push (works whether the site was just activated OR already
  Active).
- `ActivateMaterialSitePage.js` now has an optional "Cost" number input (`data-testid=
  "activate-material-site-amount-input"`) between the Site select and the Activate button.
  Leave blank -> old 0-amount-fallback behavior; fill in -> real Cost pushed.
- No standalone `/admin/material-valuation/set` endpoint or SAP Write page section anymore -
  removed after the merge (avoid duplicate reachable UI/endpoints for the same action).
- Synthetic super_admin test session used to verify (bypasses `admin_activate_material_site`
  permission check): `vms_session` cookie
  **`ALCdFh6z4UcpCBMnXmOM_vpzIy3-wI8Bg6k_YVIiJ2I`**, user
  `sapwrite.valuation.test@rampgroup.co.in`, valid 7 days from Sep 21 2026.
- Self-tested (screenshot): passcode gate -> real lookup against SAP -> clean "No material
  found" error round-trip confirmed live. Did NOT attempt a real successful write against a
  genuine material (permanently changes live SAP financial data) - user should test the real
  write themselves (e.g. Material 368 @ P4/P6/P7 from their screenshot) via this page.

## BUG FIX: PO-from-PR item match fails on case mismatch (Sep 21 2026, this session)
Real user report: purchase request icode "POLY5x7LD" showed "Not matched in SAP" during PO
creation from PR, blocking the PO - even though the material genuinely exists and is active.
- Root cause: SAP's real material InternalID is "POLY5X7LD" (uppercase). Both matching paths in
  `po_pr_lookup` are exact-string comparisons: the `inventory_cache` `$in` filter is case-sensitive
  (Mongo default - live-confirmed empty result for the mixed-case icode, non-empty for the
  uppercase one), and the live SAP fallback (`sap_material_client.resolve_material_info`, a `>=`
  SelectionByInternalID boundary query) also missed it - uppercase letters sort BEFORE lowercase
  in SAP's own comparison, so a lowercase 'x' in the search string sorts AFTER the real
  uppercase-only record, silently excluding it from a ">=" search (live-confirmed:
  `resolve_material_info("POLY5x7LD")` -> no match, `resolve_material_info("POLY5X7LD")` -> real
  match, uuid a914bea5-2316-1ede-81ed-f4ba8496d7cd).
- Fix: `po_pr_lookup` now normalizes every PR `icode` to uppercase (`.strip().upper()`) before
  BOTH the `inventory_cache` match and the live SAP fallback lookup - matches this app's existing
  convention elsewhere (e.g. `stock_transfer_service.get_product_stock_locations`). The displayed
  `icode` field (shown to the user) is untouched - only matching uses the normalized value.
  `matched_product_id` (what the frontend's `PurchaseOrderPage.jsx` actually sends to
  `/purchase-orders/create`, confirmed via grep - never the raw icode) now correctly returns
  SAP's real uppercase code either way.
- Live-verified: both the cache aggregation and the SOAP call now return the real match for
  "POLY5X7LD" after normalization. Backend restarted clean, no errors.
- User-confirmed fixed after a fresh PR re-fetch (their first retest hit stale frontend state
  from before the fix - a full page refresh + re-fetch resolved it).

## FEATURE: live PO item-number verification before GRN submission (Sep 21 2026, same session)
Follow-up to the S000073/PO 29581 investigation above - proactive safety net so the exact same
"Incorrect purchase order reference Item UUID" failure surfaces with a clear, specific reason
BEFORE the SOAP call, instead of a generic SAP rejection after the fact.
- New `supplier_shipment_service._verify_po_items_live(sap_po_client, po_number, item_products)` -
  live-queries SAP (`sap_po_client._fetch_between`) for the PO's CURRENT item list right before
  GRN submission and flags any cached item_number that either no longer exists or now maps to a
  different product than expected.
- `group_items_by_po_for_gr(doc, sap_po_client=None)` now runs this check per PO, drops any stale
  item from `item_products` (reusing the existing "missing product" skip-this-line-only logic
  every caller already has) and records the real reason in a new `item_skip_reasons` dict.
- All 3 GRN submission paths in `sap_playwright_supplier_pgr_service.py` (`_post_one_po`/
  `post_goods_receipt_via_ui`, `create_inbound_delivery_notifications_only`,
  `create_and_release_inbound_delivery_notifications`) now surface `item_skip_reasons` in their
  error/skipped_items messages instead of always assuming "Product ID missing".
- server.py's 3 call sites of `group_items_by_po_for_gr` now pass the existing `sap_po_client`
  global. Live-verified directly against real PO 29581/items 8+9: correctly returns "Item 8 no
  longer exists on PO 29581 in SAP..." (matches the real incident exactly). PO 29581's own
  shipment (S000073) was left as permanently skipped per user's explicit choice - not manually
  remapped.

## BUG FIX: "No movement" GRN policy was only applied to ONE button, not all paths (Sep 21 2026)
Real user report on shipment S000073 (multi-PO): most lines showed "Posted (movement failed)"
even though the real SAP Goods Receipt succeeded (8/10 items genuinely inbound, SAP Inbound
Delivery # assigned) - only 2 items (PO 29581) were genuinely never received (permanently
"Skipped", likely a Cancelled PO/item, same class as the PO 25271 incident).
- Root cause: the Sep 20 2026 "eliminate any warehouse movement, only complete GRN" decision was
  only wired into the single "Post GRN in SAP" button (`_start_full_auto_grn_job` passing
  `skip_movement=True` explicitly). 3 other paths still ran `_post_goods_movement_for_items`
  automatically: the normal Playwright multi-PO approval flow, Manual GRN's "Re-check SAP"
  (`check_manual_gr_quantities`), and the background Delivery-ID auto-fetch
  (`manually_confirm_inbound_delivery`).
- Fix: `finalize_goods_receipt`'s `skip_movement` param default flipped `False` -> `True` (covers
  the normal approval flow + Re-check SAP, both call it with no override).
  `manually_confirm_inbound_delivery`'s own direct `_post_goods_movement_for_items` call replaced
  with the same `sap_movement_status: "not_applicable"` outcome, matching the button's behavior.
- Left untouched (user's explicit ask was "all remaining GRN **paths**", i.e. automatic triggers)
  the manual "Retry Movement" button/`retry_goods_movement` endpoint - a user-initiated action
  that only ever shows for OLD shipments already stuck on `sap_movement_status: "pending"` from
  before this fix; going forward no new shipment will ever reach that state so the button is
  effectively dormant for new GRNs, same "leave historical data/legacy paths alone" pattern
  already used elsewhere in this app (e.g. paused ERP-sync STOs).
- Backend restarted clean, no errors. Not yet run through `testing_agent` (backend-only default-
  param + one dead-code-path removal, verified by code inspection across every call site via grep).

## FEATURE: 2-step "Validate STO" then "Review & Create STO" (Sep 21 2026, same session)
User's explicit ask, directly targeting the STO-526/527 class of live incident above - split STO
creation into 2 clearly-labeled steps so nothing writes to SAP until stock/valuation/activation
are all confirmed clean first.
- New `stock_transfer_service.validate_stock_transfer_order()` - ZERO side effects (no Mongo doc,
  no stock relocation, no SAP Maintain write): (1) cache-based structural resolution (reused via a
  new extracted helper `_resolve_and_validate_items`, same rules `create_stock_transfer_order`
  already enforced, behavior unchanged there), (2) LIVE SAP stock check on ONLY the exact
  warehouse+items the user entered (user's explicit ask - no broader "better source" suggestions),
  (3) destination-site Valuation check (reuses today's earlier proactive check), (4) Activation/
  Planning check via SAP's own read-only `check()` dry-run (already existed, used later in the
  flow - just moved earlier, zero side effects). Per user's explicit ask, only issues that would
  truly make SAP reject the order come back as blocking `level: "error"`.
- New `POST /api/stock-transfer/validate` endpoint (server.py).
- Frontend (`StockTransferPage.js`): single "Review & Create" button split into "1. Validate STO"
  (must be clicked and pass clean first) and "2. Review & Create Stock Transfer Order" (disabled
  until Validate passes). Validation resets automatically if items/site/location/date change
  afterward. Failures show in a modal "error box" (plain-language, one bullet per issue) with an
  OK button to dismiss.
- Tested via `testing_agent`: all 7 scenarios pass (gating, validating/validated states, reset-on-
  edit, client vs server validation split, error box, existing Create flow regression, Recent
  Orders list regression) - iteration_190.json, zero bugs found.
- CORRECTION to an earlier same-session misdiagnosis: when investigating STO-526/527, "P1-MOV"
  shown in the live app was wrongly assumed to indicate an out-of-sync/outdated deployment. It is
  NOT - `movDisplayName()` in StockTransferPage.js deliberately renames "{SITE}-HOLD" to
  "{SITE}-MOV" for display only (friendlier than "HOLD"; matches the warehouse's real SAP name,
  "MOVEMENT GODOWN-P1"). The real root cause for those 2 stuck STOs remains the stale-HOLD-cache
  issue documented above, now fixed for future STOs.

## BUG FIX: {SITE}-HOLD wrongly offered/picked as an STO's own SOURCE warehouse (Sep 21 2026)
Real live incident (STO-000526 P1->P8, STO-000527 P3->P2, deployed LIVE app, AFTER today's earlier
STO relocation fix): both reached "created_in_sap" fine, but SAP's own Delivery Proposal release
failed for EVERY line item ("Determination of source inventory failed" for P42417, P26724,
P-42152, P27784, +more; "Inventory in logistics area not available") - live-confirmed via direct
SAP inventory query that ALL involved products (P42417/P26724/P-42152/P27784/SI-1740-3/SCR4X8SAM/
WMNB51EBXL-8) have ZERO stock in their site's own {SITE}-HOLD staging warehouse right now - real
stock sits in P1-QC/P1-RM/P1-SFG/P1-RTV or P3's RM zone instead.
- Root cause: `get_product_stock_locations` (backs BOTH the "Select Source Warehouse" dropdown via
  `/stock-transfer/inventory` AND `suggest_source_warehouse`) reads from `inventory_cache`, which
  only refreshes every couple hours and does NOT exclude {SITE}-HOLD from the candidate list.
  {SITE}-HOLD genuinely does carry real (but transient) stock sometimes - live-confirmed 48 such
  rows across sites right now (e.g. P1-HOLD held 48 units of BOX-SHCM-T at last cache refresh) -
  it's mid-flight stock from ANOTHER in-flight order, about to be Goods-Issued out, not a durable
  "home". If a NEW STO's source got set to HOLD off a stale cache snapshot, `submit_order_to_sap`'s
  existing "already in HOLD, skip relocation" check (`source_warehouse_id == hold_warehouse_id`,
  from today's earlier fix) correctly trusted that as-is - but by the time SAP actually tried to
  source it, that HOLD stock had already moved on, leaving nothing there.
- Fix: `get_product_stock_locations` now excludes any location whose warehouse_id resolves to that
  site's own `{SITE}-HOLD` (same helper, `_relocation_hold_warehouse_id`, already used by the
  relocation step) - {SITE}-HOLD can never again be offered/suggested/accepted as a NEW STO's
  source warehouse. Live-verified: BOX-SHCM-T's P1-HOLD row (48 qty) is now correctly filtered out
  of its location list.
- These 2 specific stuck STOs need MANUAL resolution in SAP (relocate the real physical stock -
  P1-QC/RM/SFG for STO-526's items, P3's RM zone for STO-527's - into {SITE}-HOLD directly, or
  release the Warehouse Request against the real location) - this fix only prevents the same
  mistake on FUTURE new STOs, it does not retroactively repair these two.
- Not yet run through `testing_agent` - live-verified via direct DB/SAP query only.

## BUG FIX: STO "insufficient stock" for already-relocated stock (Sep 21 2026, this session)
Real user report: creating an STO for an item already activated + already physically moved to
the SFG/{SITE}-HOLD area still failed with an insufficient/negative-stock rejection.
- Root cause: a PRIOR session had already added shortfall-check logic to
  `_relocate_items_to_source_hold_warehouse` (checks {SITE}-HOLD's own current balance via
  `sap_inventory_client` and only moves the shortfall, or skips entirely if HOLD already covers
  the requested qty) - but BOTH call sites never actually passed `sap_inventory_client` through:
  `submit_order_to_sap`'s own call to that helper (stock_transfer_service.py) AND server.py's
  call to `submit_order_to_sap` itself. So the check silently always defaulted to skipped, and
  the code kept trying to move the FULL requested qty out of the original source warehouse -
  which SAP correctly rejected since that stock had already been relocated there previously.
- Fix: wired `sap_inventory_client` through both call sites (server.py's `_run_submit_sto_to_sap_job`
  -> `submit_order_to_sap` -> `_relocate_items_to_source_hold_warehouse`). Only one call site of
  `submit_order_to_sap` exists (used by both initial creation and Retry, confirmed via grep), so
  this single fix covers both paths.
- Verified via direct Python test (mocked SAP clients): simulated a product already fully
  relocated to `{SITE}-HOLD` (10/10 qty) - confirmed the goods-movement call is now correctly
  SKIPPED instead of attempting (and failing) a full-qty move from the now-empty source warehouse.
  No live SAP write was needed to prove this (pure wiring bug, logic itself already existed and
  was previously live-verified). Backend restarted clean, no errors.

## FEATURE: 2-step "Validate GRN" then "Post GRN in SAP" (Sep 21 2026, this session - fork continuation)
- Mirrors the STO 2-step pattern above. Backend (`validate_grn` in `supplier_shipment_service.py`,
  route `POST /admin/grn/{doc_code}/validate` in `server.py`) already existed from a prior fork -
  runs 4 concurrent checks via `asyncio.gather` (PO item cancellation, open qty, site activation,
  valuation), zero side effects. This session added the frontend wiring in `GrnApprovalPage.jsx`:
  Step 1 "Validate GRN" button -> Step 2 "Post GRN in SAP" button (disabled until validated AND
  Supplier Invoice Number + Bill Date filled). Editing Actual Qty or Site resets validated state
  (stale-invalidation useEffect). Validation error modal (`grn-validation-error-box`) lists issues.
- Tested via testing_agent (iteration_191): 12/12 assertions passed, no bugs found. Live validate
  call returned in ~1.5s on a real shipment - well under the user's <10s target.
- Both STO and GRN 2-step Validate/Create flows are now FULLY COMPLETE end to end (backend+frontend).

## STO Receive: Playwright REMOVED entirely (Sep 21 2026, this session, user's explicit direct instruction)
Real live failure (STO-000131/P1D1-568, 2-line order: "Delivery still shows as Not Released after
Save and Close") triggered the user's explicit ask to STOP driving Playwright/SAP-UI-login for STO
receiving completely. Replaced with a pure API-based flow:
- `inbound_receipt_service.prepare_receipt` now also requires `outbound_delivery_ids` non-empty
  ("SAP delivery reference available" gate) and `items` non-empty, else 400.
- New `receive_stock_transfer_order` calls `_relocate_receipt_from_hold` (now accepts optional
  `quantity_overrides`) as the ONLY action - a real SAP Goods Movement (plain OData, no login, no
  browser) moving stock from wherever GR lands ({SITE}-RM/-HOLD per `inbound_staging_area_for_site`)
  to the STO's real destination. No more "Post Goods Receipt" SAP UI screen at all for STOs.
- `server.py`'s `/inbound-receipts/{sto_id}/receive` no longer calls `sap_playwright_pgr_service`/
  `_run_playwright_job_with_retries` - calls `receive_stock_transfer_order` directly. Frontend
  (`InboundReceiptsPage.js`) needed ZERO changes - already rendered `receipt_relocation.lines`
  (GM ID per line / error) exactly this way as a second step; now it's the first and only step.
- `sap_playwright_pgr_service.py` itself is NOT deleted (still used by an unrelated GRN admin retry
  endpoint) - only STO Receive stopped calling it.
- Tested via testing_agent (iteration_192): 6/6 backend tests passed. 400 gates verified (no GI
  posted, no delivery reference, unknown STO). Confirmed via logs: zero Playwright/browser lines
  for inbound_receipt jobs. Live-tested directly this session against STO-000131 (~25s, no
  Playwright, correct per-line SAP error surfaced). Minor edge case (empty items list) fixed
  post-review: now 400s instead of silently reporting "received".

## Follow-up fixes on top of Playwright removal (Sep 21 2026, same session)
Real live incident (STO-000132, site P2, products SCR755WM/SCR525WM) surfaced 2 more bugs right
after the Playwright removal above:
1. `inbound_staging_area_for_site` default was WRONG - the "Sep 20 blanket -RM for every site"
   claim was based on a false positive (see the CORRECTION comment in `sap_wip_clearing_client.py`
   itself for the full story - "-RM" can silently succeed by pulling unrelated pre-existing fungible
   stock instead of the genuinely-just-received batch, hiding the real bug until a product with
   ZERO stock at "-RM" exposes it loudly). Reverted default to "-HOLD" for every site, keeping ONLY
   P8 ("P8-RM", user's own direct real-time confirmation) and P3 (its own bin) as overrides.
2. `retry_receipt_relocation` only ever updated `receipt_relocation`, never `receipt_status`/
   `receipt_error` - so a genuinely-fixed-by-retry order stayed stuck showing "Receipt Failed" with
   a stale message forever. Fixed to properly map the retry's own result to `receipt_status`.
3. A one-time double-movement side effect of bug #1 (SCR755WM got moved twice - once from the wrong
   P2-RM, once from the correct P2-HOLD after the fix) left P2-RM short 1 EA and P2-SFG over by 1 EA.
   Attempted an automated compensating reversal (P2-SFG -> P2-RM) 3x - SAP consistently rejects this
   specific REVERSE direction with a generic error (forward movements work fine) - **needs a manual
   correction in SAP directly, still outstanding, ask user before closing this out**.
4. UI cleanup (user's direct ask, screenshot of old Playwright-era progress bar) - removed the
   "Queued" state, fake ETA countdown (`AVG_SECONDS_PER_DELIVERY`/`etaText`), and the striped
   animated progress bar from `InboundReceiptsPage.js` - now a single plain "Moving stock…" spinner.
- Tested via testing_agent (iteration_193): 100% pass, all 3 fixes verified (warehouse defaults per
  site, retry status-update, UI cleanup DOM checks). STO-000132 confirmed "Received" with both GM IDs.
- Fixed `_relocate_items_to_source_hold_warehouse`'s stock-check query bug (STO-000129/G12LW at P1):
  `get_inventory_detail`'s `product_ids` param silently overrides `site_id`/`warehouse_ids` - was
  summing company-wide stock instead of just `{SITE}-HOLD`'s own balance, wrongly skipping the
  relocation move. Fixed by filtering returned rows by `logistics_area_id` client-side. Same fix
  applied to `validate_stock_transfer_order`'s live stock check (same bug, different call site).
- Discovered via live SAP query: **P4 and P2W have no `-HOLD` warehouse at all** (P1/P2/P3/P7/P8/P9
  do). User's decision: `SITES_WITHOUT_HOLD_WAREHOUSE = {"P4", "P2W"}` - relocation is skipped
  entirely for these, STO sources directly from the real warehouse. P1W/P5/P6 returned zero
  inventory rows (unconfirmed/inactive) - not yet in this set, add if they surface the same issue.
- Reverted the "-MOV" display rename (`movDisplayName` in `StockTransferPage.js`) back to showing
  the real "-HOLD" name, per user's direct ask.
- Fixed STO create progress dialog auto-close: previously stopped polling as soon as Goods Issue
  posted even if ERP Portal sync was still mid-retry, freezing its badge forever. Now waits for
  both to reach a terminal state, then auto-closes ~1.2s after full success (not on failure/manual).
- Corrected a prior session's misleading PRD claim that STO Inbound Receiving was "manual" - it was
  actually a working one-click Playwright automation (before being replaced by the API-only fix
  above this session) - see the "CORRECTION" note further down in this file.
- **"Query Goods And Activity Confirmations" ABAP dump - needs SAP support, NOT further app-side
  work** (updated Sep 20 2026, was "Warehouse Order/Confirmation SAP query integration" above).
  Communication Arrangement WAS enabled by user's Basis team (endpoint:
  `.../sap/bc/srt/scs/sap/querygoodsandactivityconfirma1`, operation
  `FindInventoryChangeItemOverviewSimpleByElements`, service `QueryGoodsandActivityConfirmationIn`)
  and its response schema has EXACTLY the fields needed (`GoodsAndActivityConfirmationID`,
  `InventoryManagedQuantity`, `InventoryMovementDirectionCode`, logistics area + product + stock
  status) to get real post-Put-Away ground truth. WSDL obtained and request schema figured out
  (namespace MUST be `http://sap.com/xi/SAPGlobal20/Global`, NOT the WSDL's own declared
  `http://sap.com/xi/A1S/Global` - confirmed via provider-side error log "message ... not
  supported"). **Live-proven root cause of the remaining failure**: querying with NO matching
  confirmation (e.g. a nonexistent/wrong ID) returns a valid empty 200 response; querying with a
  REAL, existing `SelectionByConfirmationID` (confirmed via a genuine GACID from our own successful
  Goods Movement, e.g. `280234`) causes an ABAP dump ("ASSIGN on empty generic box") on SAP's
  side while trying to serialize the actual matching record. Also confirmed the nested
  `SelectionByInventoryLocationLogisticsAreaKeySiteID`/`...MaterialKeyProductID` filter paths
  crash the same way even with zero real matches. **This is a genuine bug/gap in SAP's own
  implementation for this tenant, not a request-shape problem** - next session (or the user's SAP
  consultant/support) should open a ticket with these exact repro details (working WSDL/namespace,
  the crash only on real-data SelectionByConfirmationID lookups) rather than re-attempting request
  variations from scratch.
- Phase 5: External QMS feed
- Wire proven direct-SOAP Vendor GRN posting (`sap_inbound_delivery_notification_client.py`,
  see SOAP_GRN_BREAKTHROUGH doc) into `supplier_shipment_service.py` production code,
  replacing Playwright - behind a site allowlist (P8 only for now, only site with EM1 set up).
  NOT YET STARTED this session (got diverted into the STO Outbound/Inbound investigation
  above, which is now closed/reverted).
- STO outbound "amber ERP-sync spinner stuck" UI bug (`StockTransferPage.js`, `giPollStopRef`
  logic around line 1016 stops polling the moment GI finishes, ignoring background
  `erp_sync` status) - diagnosed in a prior session, NOT YET FIXED.
- Do NOT restore single-line `PGIInBackground` Goods Issue automation if this comes up again
  (recurring temptation, count 4+) - see "Goods Issue architecture" decision above, still
  stands as of Sep 18 2026.

### P1 (blocked/paused by user)
- Landed Cost Calculation (Freight & Customs Duty) - see `/app/memory/LANDED_COST_DESIGN.md`
- Bulk Vendor Invite
- E-Way Bill JSON Export
- Movement Audit Trail
- Stuck Request Alert
- QMS-to-GRN Link
- Auto-Lookup After Release
- Add Line Button
- Model Freshness Badge
- QR Code Link
- Azure AD Login `aud` mismatch on production - USER VERIFICATION PENDING
- Model ID not visible in Confirmed Production report - BLOCKED (paused by user)

### P2 (blocked)
- "Individual Material" ID mismatch for Capital POs - blocked on SAP Admin
- HSN/SAC code not saving to SAP during Service PO - blocked on SAP functional consultant
- Enable Line-Item Deletion in SAP - blocked on SAP business config
- GL Account Favorites
- Reconcile UI Button
- Account Health Check
- Rate Drift Alert
- Serial Number Counter
- Drawing Freshness Badge & Sub-Assembly Rollup
- STO-000415 missing price bug (explicitly deferred by user)

## BUG FIX (P0): "Act as Supplier" / Supplier Dashboard PO page taking ~49s to load (Sep 22 2026)
Real user report: "act as supplier page - POs loading is taking ages". Reproduced live via curl
against vendor RAD-P2-S (336 cached PO line items) - confirmed 49.4s.
- Root cause: `get_cached_pos_with_remaining`/`get_cached_po_by_number` (`supplier_shipment_service.py`)
  called `_compute_qty_state`/`_shipped_qty_so_far` ONCE PER ITEM in a Python loop - each call ran its
  own `SHIPMENTS_COLLECTION` aggregate + a `SAP_OPEN_QTY_COLLECTION` find. A genuine N+1 query pattern
  that scales with a vendor's open-line count (336 items -> 1000+ sequential Mongo round-trips). This
  had nothing to do with the live SAP call (`fetch_open_po_quantities`, already parallelized Sep 19).
- Fix: new `_qty_breakdown_by_status_bulk()` (one grouped aggregate for the WHOLE vendor) +
  `get_sap_open_qty_bulk()` (one `$in` find) + `_compute_qty_state_from_maps()` (pure, no DB access) -
  both list endpoints now fetch these maps ONCE, then loop in-memory only. `already_shipped_qty` is now
  derived from the same breakdown map (`in_transit_qty + approved_qty`) instead of a 3rd per-item query.
- Verified live: same endpoint (RAD-P2-S, 336 items) now takes **14s** (was 49.4s) - byte-for-byte
  identical output confirmed (0 mismatches across 336 items x 4 fields: in_transit_qty, received_qty,
  remaining_qty, already_shipped_qty). Remaining 14s is genuine, already-optimized live SAP latency
  (166 distinct POs / 15-per-chunk = 12 chunks, capped at `SAP_MAX_CONCURRENT_REQUESTS=3` - a
  deliberate, documented tenant-wide limiter shared by every SAP call in the app, not something to
  raise casually). Not yet run through testing_agent - self-verified via direct timing + diff against
  the pre-fix response.

## Testing status
  generalization fix.
- Inbound generalization fix self-tested (pytest + direct mocked call-path check across
  P1/P2/P3/P8) - not yet run through testing_agent as a dedicated pass.

## Part 10 (Sep 23 2026) - Inbound Receipts redesign: detail modal, split receive/relocate, error UX fixes
User's explicit redesign ask, fully implemented + testing_agent verified (13/13 frontend, 1/1
backend pass, 0 bugs): per-row "Receive" now opens a `ReceiptDetailModal` (new file,
`components/ReceiptDetailModal.jsx`) that fires the real SAP Goods Receipt (Acknowledge+PGR)
IMMEDIATELY on open (not gated behind Confirm), overlapping that ~fast call with the time the user
spends reading the item preview; clicking "Confirm & Receive" then starts the slower ({SITE}-HOLD
-> real warehouse) relocation with its own progress step. Bulk selection (checkboxes,
"Receive Selected") REMOVED entirely per user's ask - only single-row receive remains. Completed
tab table simplified to status badge + plain error text only - no popover, no inline retry button;
a "View" button opens the same modal read-only with a Retry button if partial/failed.

Backend split: `inbound_receipt_service.start_automated_receipt` now ONLY does Acknowledge+PGR
(phase 1), sets `receipt_status="awaiting_relocation"` when done, no longer calls relocation
internally. New route `POST /api/inbound-receipts/{sto_id}/relocate` (phase 2) branches to
`receive_stock_transfer_order` (first attempt) or `retry_receipt_relocation` (already attempted)
- reused by both the modal's auto-flow and its Retry button.

Also fixed in this same session (real bugs found via user report "quantity doesn't get posted" +
live DB inspection of failed/partial STOs):
1. `retry_receipt_relocation` used to re-submit EVERY line to SAP on retry, including
   already-successful ones - risked overwriting a real gac_id with a false failure, or
   double-moving stock. Now only re-attempts lines that weren't `ok` last time; already-successful
   lines' results (real gac_id) are preserved untouched. Verified twice: once with a stub client on
   a copied doc, once for real against live STO-000084 (G12NUT/G12FW gac_id 282833/282834 stayed
   byte-identical across a real retry call).
2. `_clarify_goods_movement_error` (store_approval_service.py, shared by GRN + STO relocation) only
   recognized ONE raw SAP error pattern ("negative stock not permitted") - "No inventory items
   found for external id..." (same root cause, different SAP wording, real case STO-000063) was
   leaking straight through with raw internal MOV-xxx/I-xxx IDs. Now matches both patterns.
3. Error text was truncated at a hard 120 chars mid-word for multi-line summaries once several
   lines' clarified messages got joined - raised to 300 chars, cuts at a word boundary.
4. Pending tab wasn't humanizing `receipt_error` at all (Completed tab was) - now consistent.
5. User feedback round 2 on the new modal: (a) error said generic "the source warehouse"/"STO
   warehouse" - now names the actual warehouse ID (e.g. "P8-HOLD"); (b) per-line result had no
   quantity - added `quantity`/`unit_of_measure` to every line result; (c) no SAP delivery number
   (PxDx) visible - added `inbound_delivery_ids`/`outbound_delivery_ids` to both list endpoints,
   shown at the top of the modal title.

Known non-blocking rough edges (testing_agent code-review notes, not bugs - not yet actioned):
- Partial STOs currently show in BOTH Pending and Completed tabs (both queries include "partial").
- View mode has no Retry for a phase-1-only failure (receipt itself failed, no relocation attempted
  yet) - user must go back to Pending tab, where the same STO is still actionable via "Receive".
- `list_pending_receipts`'s on-the-fly SAP delivery-ID backfill can occasionally push /pending's
  response past 30-60s on a cold cache (pre-existing, not from this session's changes).
- Environment note: hit a real ENOSPC file-watcher crash-loop this session (unrelated to this
  feature) - fixed by adding `CHOKIDAR_USEPOLLING=true` to frontend/.env. Webpack's own Watchpack
  (src/ hot-reload) still logs non-fatal ENOSPC warnings - if a frontend code change doesn't seem
  to appear live, do a manual `sudo supervisorctl restart frontend` rather than assuming hot reload
  applied it.

## Part 6 pointer (Sep 22 2026) - STO relocation speed investigation, SAP OData $batch ruled out
Full detail in `/app/memory/sto_receipt_performance_investigation.md` section 9. User asked to
re-check `InventoryNotification.html`/`SiteLogisticsRequest.html`/`khgoodsandactivityconfirmation.xml`
for a genuine per-line-independent OData `$batch` path to beat the SOAP atomic-batch limit (#6 in
that doc). Both HTMLs ruled out (read-only/3PL-only, and status-only respectively).
`khgoodsandactivityconfirmation` looked promising (live-confirmed deployed, `IS_CREATABLE=true` on
both the header and item entities, a real double-entry `TransferGroupID` line model) but a live
read-only test found `$expand=InventoryChangeItem` (or querying `InventoryChangeItemCollection`
directly) throws HTTP 500 (ABAP dump) on this tenant - the SAME underlying bug already documented
below ("Query Goods And Activity Confirmations ABAP dump"), just reached via OData instead of SOAP.
Since a Create/POST would need SAP to serialize the same item node back in its response, this is a
near-certain landmine on write too (not live-tested on write, per user's explicit choice to stop
here). **User's decision: accept ~15-20s as the realistic target for a 10-line STO** (real SAP
per-call latency, ~7-9s/call, is the hard floor - connection pooling can't fix that) and ship the
safe win instead - added a persistent `requests.Session` to `SAPGoodsMovementClient.__init__`
(`sap_goods_movement_client.py`), used by both `session.post()` in `goods_movement()`. The client is
a module-level singleton (`server.py`) reused by every concurrent relocation line
(`ThreadPoolExecutor` in `inbound_receipt_service._relocate_receipt_from_hold`), so this genuinely
pools TCP/TLS connections across calls instead of a fresh handshake each time - `requests.Session`
is documented thread-safe for concurrent use. Self-verified via a live dry-run call (same session
object reused across 2 calls, envelope unchanged) - backend restarted clean. Not run through
`testing_agent` (single-file, non-behavioral connection-pooling change, zero API/schema change) -
should be observed on the next real multi-line STO receipt for the actual timing improvement.

## Part 5 pointer (Sep 22 2026) - STO Inbound GR fully automated for task-supported sites (P1)
Full detail in `/app/memory/CHANGELOG.md` "Part 5" - built a new custom SAP OData service
(`khinbounddeliveryexecution`) + Event Notification webhook (subscribed to the CORRECT Business
Object, `Site Logistics Lot`, not the wrong one from the Part 4 dormant plan) to automate
Acknowledge -> Release -> (SAP creates a Warehouse Order) -> webhook -> `ConfirmAsPlanned` ->
verified real inventory posting -> existing HOLD relocation. Live-tested multiple times, real SAP
writes, real inventory confirmed each time. `testing_agent` iteration_194: 8/8 pass, no bugs.
**NOT YET observed end-to-end through one single real "Receive" click on a genuinely fresh,
DB-tracked P1-bound STO** (every currently-pending one had already been auto-Released/Finished by
SAP's own routine processing) - do this the first time a fresh one shows up in the Pending tab.

## Part 4 (Sep 22 2026, this fork continuation) - GRN warehouse-movement policy REVERSED, ERP
sync resumed, SAP push (webhook) groundwork. Full detail in `/app/memory/CHANGELOG.md` "Part 4"
section - this is a pointer + the critical NOT-YET-TESTED list for whoever picks this up next.

- **GRN movement reversed** (user's explicit ask - SAP now routes every Goods Receipt into
  `{SITE}-HOLD` first, tenant-wide): `finalize_goods_receipt` default `skip_movement` flipped
  back to `False`; `manually_confirm_inbound_delivery` and `_auto_finish_full_auto_grn`
  (background Put Away retry loop) both run real movement again. Real bug found + fixed along
  the way: frontend's `warehouseId` was hardcoded `""` since Sep 20 (old policy's belt-and-
  suspenders guard) - fixed at the source of truth (`prepare_approval` now defaults blank
  `warehouse_id` via `_SITE_RM_WAREHOUSE_OVERRIDE.get(site_id, f"{site_id}-RM")`, reusing
  `store_approval_service.py`'s existing override map). P3 confirmed live: `P3-Z1-01-A` (no flat
  `P3-RM` exists in SAP). **Verified live end-to-end on ONE already-stuck shipment only**
  (S000040, manually patched + retried - real GM IDs 281790/281841 posted). **NOT YET verified**:
  a brand new shipment going through the full automated path (approve -> GR post -> background
  loop -> Put Away confirm -> movement) with zero manual intervention - do this first before
  trusting the automated path for real.
- **STO_ERP_SYNC_PAUSED resumed**: flipped back to `"false"` (user confirmed). The 26 STOs that
  piled up `erp_portal_status: "paused"` during the pause were deliberately left untouched, NOT
  synced - user must explicitly decide their fate later if ever revisited.
- **SAP push (webhook) groundwork built, NOT wired to anything yet**: dormant
  `POST /api/webhooks/sap-put-away` receiver (Basic Auth, logs raw payload only), new
  `POST /api/admin/grn/{doc_code}/check-now` (on-demand single-cycle check) + "Check Now" button,
  faster progressive backoff `[5,10,15,20,30,30,30,30]` in the Put Away retry loop, frontend
  polling tightened 30s->8s while pending. SAP-side Event Notification subscriber intentionally
  left **Inactive/unsaved** by user - confirmed correct Business Object is `SiteLogisticsTask`
  (Updated event) if/when this gets activated. Full rollout plan for SAP Admin/Basis:
  `/app/memory/sap_event_push_plan.md`.
- **TESTING GAP, be aware**: none of Part 4's changes went through `testing_agent` or a UI
  screenshot - only backend curl + one real live SAP write (S000040's manual retry) + code
  review. The frontend changes (Check Now button, movement banner suffix, progress bar label,
  tightened polling) have NOT been visually/interactively verified at all yet.
- Recurring, not-a-real-bug: backend went unresponsive twice more this session after routine
  file edits - Uvicorn `--reload` watcher hanging (known issue, not this session's code).
  Fix is always `sudo supervisorctl restart backend` (~5s recovery each time).

## Sep 18 2026 session
- STO Site Logistics automation (SOAP EM2) was built then EXPLICITLY REVERTED per user
  choice - manual "Create Outbound Delivery with release" SAP UI step is kept intentionally.
- Fixed: PO form auto-derive (reactive useEffect on `sites`), missing PO 29654 in supplier
  dashboard (backfill loop infinite-stop bug), deleted BOM component CAR136.510 still showing
  in live stock checks (`force_live` now refetches true SAP BOM structure).
- Sep 18 2026 follow-up fix: the earlier `force_live` fix only corrected the ONE live-check response, never the stored `bom_node_cache` doc itself - so a removed component kept reappearing on the panel's DEFAULT/cached view even after a live check proved it gone, until that collection's separate scheduled refresh cycle reached it (user reported this still live in production after deploying the first fix). Now a successful live fetch also self-heals the cache immediately (`bom_cache_service.persist_live_fetch`). Reproduced with an injected stale fake component and confirmed fixed via curl (cache showed it -> live excluded it -> cache re-check no longer showed it).
- Root-caused (NOT a code bug, per user's explicit "no change needed" then "resolved for
  now") the Manual GR mismatch for shipment PY8AL8/PO 29482/product WAS8PZ: SAP's own
  Inbound Delivery analytics consistently report 9 ea confirmed vs 8 ea recorded in the
  portal - a genuine physical discrepancy, not a parsing bug. User resolved manually via the
  existing admin unblock panel.
- Built "Act as Supplier" feature: new `act_as_supplier` permission (IT Access Management,
  Supplier Management group). Lets internal staff (1) pick any approved supplier and create a
  shipment on their behalf via the same PO-based flow (`ActAsSupplierPage.jsx`, endpoints
  under `/api/admin/act-as-supplier/...`), tagged `created_on_behalf_by` (flag visible on both
  GRN Approval and the supplier's own Shipments page), and (2) set + email a new password to a
  supplier account. testing_agent iteration_183: 14/14 backend tests pass, full frontend flow
  live-verified, no bugs found.
- Sep 18 2026 bug fix (real user report, first live use of "Test Full Automated GRN" -
  shipment S000007/PO 29685, vendor RAD-P2-S): SAP accepted the MaintainBundle(release=True)
  call with no error severity and echoed the notification ID back, but had SILENTLY DISABLED
  the Release action server-side ("Action RELEASE not possible; action is disabled") - the app
  wrongly marked it "posted" anyway since maintain_bundle's own checks never required a real
  UUID/confirmed delivery. Fixed: `create_and_release_inbound_delivery_notifications` now
  requires SAP's own confirmation report to actually show the release (same source of truth
  "Re-check SAP" uses) before ever reporting "posted", with one short retry for async lag.
  Root cause of WHY release was disabled for this specific PO/vendor is still unknown (not a
  site/entity mismatch - PO 29346 that worked yesterday and this failing PO 29685 are both
  site P8, buyer entity RI) - needs the user to check the stray draft document directly in SAP
  UI. User chose not to revert shipment S000007 (will create a new test shipment instead).
- Investigated (no code change, per user's request) why an ERP PR line "Not matched in SAP":
  the ERP's item code for that material genuinely differs from SAP's real code (data entry
  issue in the ERP, not a matching bug).
- Investigated (no code change, per user's request) why a shipment's SAP "Actual Delivery
  Date" showed the approval date instead of the Bill Date: the SOAP service we use
  (`ManageStandardInboundDeliveryNotificationIn`) has no ActualDeliveryDate field at all -
  only `ManageSiteLogisticsTasks` (the STO automation the user reverted) has it. User
  decided how to proceed is still pending (asked, then moved to next task).
- Added "Sap outbound no" (SAP's human-readable Outbound Delivery ID, e.g. P8D1-172) to the
  Delivery Challan print PDF and Excel export - `stock_transfer_service.get_delivery_note_data`
  now looks up + caches it via `sap_outbound_delivery_client.find_outbound_delivery_ids`
  (previously dead code, unused since Aug 27) onto `outbound_delivery_display_ids`.
  Live-verified end-to-end for STO-000015 (real SAP call returned P8D1-172), confirmed in both
  the print page and the Excel file, self-tested via curl/screenshot.
- Sep 18 2026: GRN Approval fixes - (1) Bill Date now shows dd-mm-yyyy always (custom
  Popover+Calendar picker replacing the native `<input type="date">`, whose display format was
  silently locale-dependent, e.g. mm/dd/yyyy in the user's browser); (2) added missing "Item
  Code" column (product_id) to the shipment lookup table. Screenshot-verified end-to-end.
- Sep 18 2026: added a SEPARATE "Test Full Automated GRN" button (purple, POST
  /admin/grn/{doc}/approve-full-auto) on the GRN Approval page for the EM1 SOAP breakthrough -
  does a REAL, irreversible create+release=True SAP post in one click, no manual SAP step,
  same local Goods Movement completion as the normal flow. Server-side site allowlist
  (`FULL_AUTO_GRN_SITE_ALLOWLIST={'P8'}`) - only enabled/usable for site P8 (only site with EM1
  Logistics Model configured), disabled elsewhere with a clear tooltip/400 error. Also added a
  shared confirmation dialog (showing Invoice Number + Bill Date) now gating BOTH this new
  button AND the existing normal "Create SAP Notification" button. testing_agent iteration_184:
  100% pass, no bugs - real SAP post was deliberately never triggered during testing (Cancel
  only) to avoid an irreversible production write; user will trigger it themselves.

## Sep 19 2026 session - 3 real bugs found via live user testing of "Test Full Automated GRN", all FIXED + verified
- **Bug 1 (P0, user-reported via screenshot): SAP Put Away Task fulfilled qty stayed 0.**
  Root cause: `maintain_bundle(release=True)` only creates+releases the Inbound Delivery
  Notification - SAP's EM1 "one-step receiving" model then auto-generates a whole downstream
  chain (Warehouse Request -> Warehouse Order -> Inbound Delivery -> "Put Away" Warehouse Task),
  but nothing ever CONFIRMED that Put Away task, so "Fulfilled Quantity" stayed 0 forever
  (Planned Quantity was always correct). Fix: revived the previously-dormant
  `sap_site_logistics_client.py` (`ManageSiteLogisticsTaskIn.MaintainBundle_V1`) - a DIFFERENT
  use case than the outbound-STO one it was originally built+abandoned for. Rewrote
  `find_tasks_for_site` to parse full MaterialInput/MaterialOutput line detail, and rewrote
  `confirm_tasks_bundle`'s payload against SAP's real schema (2 bugs found only by live-testing,
  since SAP gives zero detail on a generic "Web service processing error": (1) `SiteLogisticTaskID`
  is mandatory alongside the UUID, was missing; (2) an empty `SourceLogisticsAreaID` tag on
  MaterialInput must be OMITTED, not sent blank; (3) `<BasicMessageHeader/>` must be present even
  though docs call it optional). LIVE-VERIFIED: real stuck Put Away Task 69040 (PO 29685, site P8)
  confirmed successfully - SAP returned SeverityCode "S"/"Saved Successfully", task then dropped
  out of the open-tasks query.
- **Bug 2 (user-reported: "still no delivery ID"): full-auto path never captured SAP Inbound
  Delivery #.** `create_and_release_inbound_delivery_notifications` never extracted
  `inbound_delivery_id` at all (unlike the older Playwright path). Fixed with the same
  confirmation-report "latest wins" extraction already used elsewhere.
- **Bug 3 (self-inflicted regression from fixing 1+2, user-reported: "60 seconds for 2 line items,
  can we speed it up"): both fixes were first tried INLINE with blocking retry loops** (SAP
  queries in this tenant cost 4-5s+ EACH, live-measured, and the retries multiplied that several
  times over). Fixed properly: `create_and_release_inbound_delivery_notifications` now does ONLY
  the one real SAP write and returns immediately (back to ~5-10s baseline). Put Away confirm +
  Delivery ID lookup both moved to `server.py`'s new `_auto_finish_full_auto_grn` background task
  (fire-and-forget after the job already reports "done", retries every 30s for ~2 min) - same
  "eventually consistent" pattern the pre-existing manual "Fetch from SAP" button already used
  (which itself documents SAP as taking 30-100s+ per PO - genuine SAP-side lag, not fixable from
  our side, just no longer blocks the user).
- **Separate P1 fix (approved by user, "1a"): Supplier PO fetch was 20-25s.**
  `sap_po_analytics_client.fetch_open_po_quantities` chunked PO fetches in batches of 15 but ran
  them SEQUENTIALLY. Parallelized via `ThreadPoolExecutor(max_workers=SAP_MAX_CONCURRENT_REQUESTS)`
  (still honors the shared `sap_semaphore` 3-concurrent-SAP-calls cap). Live-verified: vendor
  RAD-P2-S (168 POs) went from ~20-25s to 9.65s.
- testing_agent iteration_185: 100% pass (3/3 pytest), no critical/minor issues. Verified via
  code review (non-blocking refactor correctness, job "done" ordering independent of the
  background task, scoped Mongo update in `mark_put_away_confirmed`) + one live speed check on
  the read-only PO endpoint. Did NOT re-trigger a real "Test Full Automated GRN" POST (already
  live-verified by main agent this session, irreversible SAP write).
- **Separate bug fix (real user report: "why is PO 29724 not visible to the supplier dashboard"):
  off-by-one in `sap_po_client._discover_current_max_po_id`.** `_has_po_id_greater_than(N)` means
  "a PO with ID > N exists" - the binary search's own invariant converges `lo` to `true_max - 1`
  and `hi` to `true_max` itself, but the function used to `return lo`. Effect: whichever PO
  happens to be the CURRENT highest ID in the whole SAP tenant was always exactly one PO short of
  the discovered "current max", so it only ever appeared retroactively once ANOTHER, even newer PO
  was created afterward (which is what finally pushed the watermark past it) - on a slow day for
  new POs, the single latest one could stay invisible indefinitely, with no error anywhere. Fixed
  to `return hi`. Live-verified: PO 29724 (today's actual highest PO ID, vendor RAD-P2-S, 5 line
  items, genuinely open/"Sent") went from watermark stuck at 29723/0 rows found to fetched and
  cached correctly (5/5 line items) immediately after the fix.

## Sep 20 2026 session - S000013 Goods Movement rejection FIXED + live-verified (P0, carried over from prior fork)

**Symptom**: shipment S000013 (PO 29724, 5 line items - BOX706025/BOX757520/BOXPWCL4/G12LW/G12FW,
site P8, target warehouse P8-RM) had its Put Away Task confirm correctly (Fulfilled Qty matched
Planned Qty, SAP's own Document Flow showed Warehouse Task 69092 "Finished" + Warehouse
Confirmation 280174 "Not Canceled"), but the follow-up Goods Movement (step 2, moving stock from
wherever Put Away landed it into the shipment's chosen warehouse) rejected EVERY one of the 5
items with `SAP rejected the movement: No inventory items found for external id MOV-... item id
I-...`.

**Investigation (live, against real SAP data, no assumptions)**:
- Read `doc.sap_gr_result.per_po[0].put_away_target_areas` for S000013: ALL 5 products reported
  `"P8-HOLD"` (this is the value `_confirm_put_away_task`, in `sap_playwright_supplier_pgr_
  service.py`, extracted from SAP's own `TargetLogisticsAreaID` field on the Put Away Task's
  MaterialOutput lines, queried via `SAPSiteLogisticsQueryClient.find_tasks_for_site`).
- Live-queried `sap_inventory_client.get_inventory_detail(warehouse_ids=["P8/P8-HOLD"])`: returned
  stock for 5 OTHER unrelated products (P16097-B, 092018-PF, 09200726-01, SH10.0HR, IRON-SCR) but
  **zero rows for any of the 5 S000013 products, under ANY stock status**. So P8-HOLD genuinely
  never received this stock - `put_away_target_areas` was WRONG for this shipment.
- Live-queried `sap_inventory_client.get_inventory_detail(product_ids=[<all 5>])` (unscoped, whole
  tenant): every one of the 5 products already had comfortably MORE than the shipped/received qty
  sitting in `P8/P8-RM` - the shipment's actual chosen target warehouse. E.g. BOX706025 needed 3 EA
  moved, P8-RM already held 29 EA; G12FW needed 3 EA, P8-RM already held 9 EA. Same pattern for all
  5 lines.
- **Conclusion**: the stock had already landed DIRECTLY in the final warehouse (P8-RM) with no
  staging step, exactly like the Sep 19 2026 fix already found for the SAME 3 box products on
  shipment S000012 (see that entry above) - except this time SAP's own Put Away Task query
  reported the WRONG target (`P8-HOLD` instead of `P8-RM`) for that exact same physical outcome.
  This proves the Put Away Task's `TargetLogisticsAreaID` field, while usually right, **cannot be
  trusted as the sole source of truth** for where SAP's real Warehouse Order/putaway execution
  actually placed the stock - it can diverge run-to-run for the same products/site.

**Fix** (`/app/backend/supplier_shipment_service.py`):
- Replaced `_resolve_source_stock_status` (which only checked whether the HINTED area had matching
  stock, and returned blank/empty on any mismatch) with a new `_find_actual_source_area(
  inventory_client, site_id, product_id, qty, target_warehouse_id, hint_area)` that queries REAL
  on-hand inventory for the product across the WHOLE site (not just the hinted area) and:
  1. If the target warehouse itself already holds >= qty -> returns "already at target", no
     movement needed (mirrors the existing `source_area == warehouse_id` shortcut, just now
     proven by live stock instead of assumed from the Put Away hint).
  2. Else if the hinted area is among the areas holding >= qty -> uses it (still correct most of
     the time, keeps existing behavior for shipments where the hint IS accurate).
  3. Else falls back to whichever OTHER area actually holds >= qty.
  4. Only falls back to the raw hint (with blank stock status, same as before) if truly nothing
     matches anywhere - preserves the original "let SAP reject it and surface the real error"
     behavior for genuine data problems, rather than silently swallowing them.
- `_post_goods_movement_for_items` now calls this instead of resolving `source_area` from
  `put_away_target_areas_by_po` and doing a separate stock-status lookup - one function now owns
  both source-area discovery AND stock-status resolution, verified against live inventory.
- Old `_resolve_source_stock_status` fully removed (dead code after the replacement).

**Live verification (no mocks)**: called `supplier_shipment_service.retry_goods_movement(db,
"S000013", <real SAPGoodsMovementClient>, <real SAPInventoryClient>, "RI")` directly against
production SAP. Result: `{"ok": true}`, all 5 items `{"ok": true, "skipped": true, "note":
"Already in P8-RM on receipt - no movement needed"}` - correctly detected the stock was already in
place and skipped the (unnecessary, previously-failing) movement instead of attempting it. DB
confirmed updated: `sap_movement_status="posted"`, `sap_movement_result.ok=True`. No real SAP write
was needed/attempted for this specific shipment (all 5 lines were already correctly placed);  the
fix's value is in the GENERALIZED detection logic, which will now also correctly handle the
opposite case (a genuine staging area with wrong hint) by finding the real area and actually
issuing the movement there instead of failing.
- Not yet run through `testing_agent` as a dedicated pass this session (backend-only Python logic
  change, verified directly against live SAP + live Mongo state instead, per user's original
  request to use "backend testing agent / manual python -c").

## Sep 20 2026 SAME-DAY CORRECTION - the fix above was WRONG, reverted + replaced with the real fix

User pushed back with a real SAP screenshot (`Warehouse Confirmation Overview: 280188`, referencing
PO 29724 / Warehouse Order 101571 / Inbound Request 86431) showing SAP's own double-entry Put Away
posting: `+` lines into Logistics Area **P8-HOLD** for BOXPWCL4/G12LW/G12FW (3/2/2 EA), `-` lines out
of the generic receiving "Location" for the same qty/materials. This is DIRECT, document-level proof
that the Put Away Task's reported target area (`P8-HOLD`) was CORRECT all along for these products -
contradicting the conclusion above.

**Root cause of the WRONG conclusion**: the "live inventory check" used above
(`sap_inventory_client.get_inventory_detail`) reads SAP's On-Hand Inventory **analytics report** (an
OLAP cube on data source SCMINVV02), not a live transactional query. Two problems with trusting its
aggregate quantity to decide "stock is already at the target, skip the move":
1. **It can't distinguish THIS delivery's specific units from unrelated pre-existing stock.** These
   materials have no batch/serial tracking, so P8-RM already showing "more than enough" qty could
   simply be older, unrelated stock that has nothing to do with this shipment - not proof this
   delivery's units ever moved there. This is a structural flaw, not just a timing issue.
2. It may also lag behind SAP's real-time stock ledger (same class of indexing delay already
   documented elsewhere in this codebase for other SAP analytics reports).
Net effect: the Sep 20 "fix" was giving **false positives** ("already at target, skip") for
shipments where the stock was genuinely still sitting in the correct staging area (P8-HOLD) and had
simply not been moved yet - masking a real problem instead of fixing it.

**What was ACTUALLY happening with S000013**: the Put Away Task hint (`P8-HOLD`) was right the whole
time. The Goods Movement call rejecting it with "No inventory items found" was a genuine SAP-side
**ledger propagation lag** - `_auto_finish_full_auto_grn` (server.py) only waits ~8s after Put Away
confirms before firing the one-shot `retry_goods_movement` call; SAP's Warehouse Confirmation can
post successfully (as line 280188 proves) while the separate stock ledger the Goods Movement SOAP
service reads from hasn't caught up yet within that short window. There was never a wrong target
area to find - the movement just needed to be retried a bit later.

**Actual fix** (`/app/backend/supplier_shipment_service.py`):
- Reverted `_find_actual_source_area` back to the original `_resolve_source_stock_status` (trusts
  the Put Away Task's hint area directly, only resolves stock status against it - no more aggregate
  "already there" guessing).
- `_post_goods_movement_for_items` restored to resolving `source_area` from
  `put_away_target_areas_by_po` (the hint) with the plain `source_area == warehouse_id` string-match
  skip (unchanged, pre-existing, still valid - that's a literal identity check, not an aggregate
  quantity guess).
- **New**: added `retry_on_lag: bool = False` parameter. When `True`, if SAP rejects the movement
  with "No inventory items found" specifically, retries the SAME call up to 2 more times with a
  20s/40s backoff before giving up (real ledger-propagation lag, not a data problem - matches the
  same "eventually consistent, just needs a beat" pattern already used elsewhere in this codebase,
  e.g. the Put Away Task / Delivery ID lookups). Any OTHER error (wrong ID, auth, etc.) still fails
  immediately on the first attempt, unchanged.
  - `retry_on_lag=True` passed from `retry_goods_movement` (the Retry button, and
    `_auto_finish_full_auto_grn`'s post-Put-Away auto-retry) and `manually_confirm_inbound_delivery`
    (the background Delivery ID auto-fetch path) - both only ever run AFTER the Goods Receipt (and
    usually Put Away) already happened, so the lag is the realistic failure mode there.
  - Left at the default `False` (no retry, fails fast) for the very FIRST synchronous call inside
    `finalize_goods_receipt`, which runs BEFORE Put Away has even started - failing there is normal/
    expected (the real target isn't known yet), no reason to add delay to the fast synchronous path.

**Live verification (no mocks)**: re-ran `retry_goods_movement` for S000013 the same way as before -
this time with the corrected code, using the (correct) `P8-HOLD` hint directly rather than any
aggregate-inventory guess. Result: **4 of 5 items succeeded for real** (BOX757520 -> GACID 280233,
BOXPWCL4 -> 280234, G12LW -> 280242, G12FW -> 280235, all genuine SAP-assigned document numbers,
confirmed the ledger-propagation-lag theory was correct - `P8-HOLD` DID have the stock, it just
wasn't visible to the Goods Movement service on the very first attempt right after Put Away). DB
correctly reflects the honest partial state: `sap_movement_status="pending"`, `sap_movement_result.
ok=False` (NOT falsely "posted" like the earlier wrong fix claimed).

**Remaining genuine issue, item 1 (BOX706025) only**: SAP rejected with a DIFFERENT, more specific
fault this time - `HTTP 500: "Negative stock not permitted in logistics area P8-HOLD, material
BOX706025"` - not the generic "No inventory items found" from before. This is SAP correctly saying
there is truly zero/insufficient BOX706025 at P8-HOLD right now, most likely because this dummy test
PO (29724) has been reused across MULTIPLE test shipments (S000012, S000013, S000014, S000015 all
visible in the GRN Approval table) that all draw from the same underlying, non-batch-tracked P8-HOLD
stock pool for the same material - an artifact of repeated test-data reuse on a live PO, not a code
bug. Left as a genuine, correctly-surfaced SAP error (retry button still available) rather than
silently forced/faked - needs the user to confirm in SAP whether BOX706025 stock still needs a
manual top-up/correction for this specific line, or whether this line's receipt was already
consumed by an earlier test shipment's real movement.

**Lesson for future sessions**: SAP's On-Hand Inventory analytics report (`sap_inventory_client.
get_inventory_detail`) is fine for read-only DISPLAY purposes (Inventory page, BOM component stock
panel) but must NOT be used to make automated "is the stock already there" decisions for fungible,
non-batch-tracked materials - it cannot prove a specific delivery's units are in a specific place,
only that SOME stock (possibly unrelated) currently sits there. Prefer trusting the transactional
document (Put Away Task's own TargetLogisticsAreaID, Warehouse Confirmation) as the source of truth,
and handle SAP-side propagation lag with a bounded retry instead of a "verify via aggregate report"
workaround.
- Not run through `testing_agent` this session either (same backend-only Python logic scope,
  verified directly against live SAP + live Mongo state per user's original testing preference).

## Sep 20 2026 SECOND correction (same day) - real idempotency bug found, NOT a multi-inbound-per-PO problem

User pushed back again on the "test-data exhaustion" framing above with a real production concern:
"in the real entry we received multi inbound for the single PO so the issue needs to be resolved" -
correctly refusing to accept "it's just test data" as a real answer, since multiple genuine
inbound deliveries against one PO IS a normal real scenario this app must handle correctly.

**Investigation**: tried to verify via SAP's own "Warehouse Order"/"Warehouse Confirmation" object
(what the user's screenshot showed, ID 101571/280188) as the authoritative post-execution source -
confirmed this needs a brand NEW SAP Communication Arrangement (not yet integrated, would need the
user's SAP Basis team, same as the earlier Site Logistics Task setup). Also confirmed a confirmed
Put Away Task disappears entirely from `find_tasks_for_site`'s query results (live-tested: queried
128 open P8 tasks, none of the 3 already-confirmed S000013/14/15 tasks appeared) - so there's no way
to re-verify a task's real post-execution outcome through that channel either.

**While re-testing to investigate, found the ACTUAL bug**: `_post_goods_movement_for_items` had NO
per-item memory of a previous attempt. Every call (and `retry_goods_movement`/the Retry button can
legitimately be called more than once - by a user, AND automatically by
`_auto_finish_full_auto_grn`) re-attempted EVERY line item from scratch, including ones that had
ALREADY genuinely posted to SAP on an earlier attempt (real GACID document numbers: 280234, 280235,
280236, 280204 were all confirmed real successes from earlier retries in this same debugging
session). Re-sending an already-completed movement to SAP a second time correctly gets rejected
("Negative stock not permitted" once there's nothing left) - but this app was overwriting the
ORIGINAL success in `sap_movement_result` with that second, spurious failure, making a fully-
successful line look permanently broken. **This is what was actually causing most of the "Negative
stock" failures reported above - not multiple real deliveries genuinely exhausting shared stock.**

**Fix** (`_post_goods_movement_for_items`, `/app/backend/supplier_shipment_service.py`): now builds
`previous_by_item` from the shipment's OWN existing `sap_movement_result.per_item` (from the doc
passed in) before processing anything, and skips (carries the prior entry over unchanged) any line
whose previous attempt already came back `ok=True` - only genuinely still-failed or never-attempted
lines get sent to SAP again on a retry.

**Data repair**: manually restored the 4 real successes that had been overwritten by the duplicate-
retry bug before this fix landed (S000013: BOXPWCL4->GACID 280234, G12FW->280235; S000014:
BOX706025->280236, G12LW->280204) - confirmed these were genuine SAP postings from earlier in this
same debugging session, not fabricated.

**Re-tested cleanly after the fix** (one retry_goods_movement call per shipment, idempotency
confirmed working - already-`ok=True` lines were correctly skipped, not re-sent):
- S000013: 4/5 lines genuinely posted (BOX757520, BOXPWCL4, G12LW, G12FW all real GACIDs).
  BOX706025 (3 EA) still genuinely fails - "No inventory items found".
- S000014: 2/4 lines genuinely posted (BOX706025, G12LW, real GACIDs). BOX757520 (6 EA) and G12FW
  (2 EA) still genuinely fail - "Negative stock not permitted" (confirmed NOT a transient/duplicate
  issue - retried a 2nd time after the fix, same real rejection both times).
- S000015: 0/3 lines - all 3 (G12FW, G12LW, BOXPWCL4) still genuinely fail.

**Honest remaining conclusion**: after removing the self-inflicted duplicate-retry noise, there IS
still a real, smaller shortfall for some lines (e.g. G12LW: 9 EA confirmed across 3 Put Away tasks,
only 7 EA have ever successfully moved - 2 EA short) - meaning SAP's Site Logistics Task confirm
reporting "Fulfilled Quantity now matches Planned Quantity" does NOT reliably guarantee the full
planned quantity always lands as real, movable stock at the reported target area. This IS the
genuine multi-inbound-per-PO gap the user flagged, and it can only be conclusively resolved by
querying the real Warehouse Order/Confirmation execution result (the new SAP integration discussed
above, needs a new Communication Arrangement from the user's Basis team) - not fixable purely in
app logic without that ground-truth source. Flagged as a carried-over P0/P1 item, not silently
closed.
- Not run through `testing_agent` this session (same backend-only Python logic scope, verified
  directly against live SAP + live Mongo state).

## Sep 20 2026 THIRD fix (same day) - real (site, area, product) race condition + escalation, and the SAP Query Goods/Activity Confirmation dead end

**New Communication Arrangement obtained**: user's Basis team enabled "Query Goods And Activity
Confirmations" (`QueryGoodsandActivityConfirmationIn`, endpoint `.../querygoodsandactivityconfirma1`)
specifically to get real post-Put-Away ground truth (see updated P0 backlog entry above for the
full investigation) - conclusively proven to be a genuine SAP-side ABAP dump bug on THIS tenant for
any real-data lookup (`ASSIGN on empty generic box`), not something fixable from our app. Logged
in detail in the backlog for a future SAP support ticket; not pursued further this session.

**Real bug found while stress-testing the retry fix**: `_post_goods_movement_for_items` had no
protection against two shipments' movements for the EXACT same (site, logistics area, product)
running concurrently - a real risk given multiple deliveries can legitimately land in the same
staging area for the same material (as confirmed happened with S000012-S000015 all sharing PO
29724's P8-HOLD stock). User explicitly asked for a "foolproof fix" after the idempotency fix alone
still left this race condition unaddressed.

**Fix** (`/app/backend/supplier_shipment_service.py`):
- New `supplier_portal_movement_locks` Mongo collection with a TTL index (`expireAfterSeconds=180`)
  - a crashed process can never permanently hold a lock.
- `_acquire_movement_lock(db, site_id, area, product_id, max_wait_seconds)` / `_release_movement_
  lock`: simple try-insert mutex keyed on `"{site}|{area}|{product}"`. Bounded wait, never
  indefinite - callers get `None` (treated as "temporarily busy, try again shortly") if the wait
  expires, never hang forever.
- Wired into `_post_goods_movement_for_items`'s per-item loop: lock held only around that ONE
  item's actual SAP call (+ its internal lag-retries), released immediately after (success or
  failure) via `finally`. Wait budget: 5s for the fast synchronous first attempt (`finalize_goods_
  receipt`, `retry_on_lag=False`), 30s for the already-tolerant-of-lag retry paths (`retry_on_lag=
  True`) - keeps worst-case total time in the same order as before (~2 min for a genuinely stuck
  item), just slightly larger, never unbounded.
- **Escalation**: each per-item result now carries a `retry_count` (carried over and incremented
  from the previous attempt's stored count). After `MAX_MOVEMENT_RETRIES_BEFORE_ESCALATION = 3`
  genuine failures on the same line, it gets `needs_manual_check: True` + a clear note instead of
  silently staying "pending" forever - mirrors the existing `sap_gr_retry_count` pattern already
  used for Goods Receipt retries.
- `ensure_indexes(db)` updated to create the new TTL index at startup (same place all this app's
  other indexes are created).

**Live-verified** (no mocks): tested the lock directly (acquire -> second attempt correctly
returns busy -> release -> re-acquire succeeds). Re-ran `retry_goods_movement` for S000013 with the
lock/escalation code live: the 4 already-successful lines stayed correctly untouched (idempotency
still solid alongside the new lock), BOX706025 got a real, lock-protected retry attempt and is now
showing `retry_count=1` (will escalate to `needs_manual_check=True` after 2 more genuine failures
instead of looping forever unremarked). Backend restarted clean, no errors.

**Current honest end state of the 3 test shipments** (S000013/14/15, all sharing test PO 29724):
mix of genuinely-succeeded lines (real SAP GACIDs) and genuinely-still-failing lines (real SAP
"Negative stock"/"No inventory items found" - now correctly retried, locked, and will escalate
after 3 tries instead of hanging silently). Not artificially forced to "success" - this is real
SAP state, limited only by the still-open P0 backlog item above (SAP-side query bug) for full
ground-truth verification.
- Not run through `testing_agent` this session (same backend-only Python/Mongo logic scope,
  verified directly with live lock tests + live SAP retries).

## Sep 20 2026 FOURTH change (same day) - simplified GRN flow: removed movement automation + one action button globally

**User's explicit ask** (after seeing today's SAP goods-movement issues): remove the "Create SAP
Notification" button entirely (all sites), rename "Test Full Automated GRN" -> "Post GRN in SAP"
and enable it for ALL sites (was P8-only), strip the Put Away confirmation + Goods Movement
automation out of that flow entirely (GRN-only, no warehouse relocation), and remove the
"Warehouse" dropdown from the UI. User confirmed they are creating the required EM1-equivalent
SAP logistics models for all sites, so lifting the P8-only restriction is safe.

**Pre-change snapshot**: `/app/memory/pre_change_grn_buttons_snapshot.md` documents exactly what
existed before, for revert reference (use platform Rollback for an actual code revert).

**Changes**:
- `supplier_shipment_service.finalize_goods_receipt`: new `skip_movement: bool = False` param -
  when True, sets `sap_movement_status="not_applicable"` + a clear `sap_movement_result` reason,
  skips `_post_goods_movement_for_items` entirely.
- `server.py` `_start_full_auto_grn_job`: now calls `finalize_goods_receipt(..., skip_movement=True)`
  and no longer schedules `_auto_finish_full_auto_grn` (no Put Away confirm, no Goods Movement,
  no lock/retry/escalation machinery from earlier today runs at all for NEW approvals going
  forward - that machinery still exists in the code for if/when movement is re-enabled later).
  `FULL_AUTO_GRN_SITE_ALLOWLIST` removed entirely from the `/approve-full-auto` endpoint.
- `GrnApprovalPage.jsx`: removed "Create SAP Notification" button + its endpoint path entirely
  from the UI, removed the "Warehouse" Select (kept the underlying auto-fetch/auto-select state
  logic running invisibly so the API payload still gets a sensible `warehouse_id` value), renamed
  remaining button "Post GRN in SAP", removed its `siteId !== "P8"` disabled condition, updated
  the confirm dialog title/description/summary to match (Site only, no Warehouse row, explicit
  "warehouse movement is not performed" text).

**Tested via `testing_agent`** (iteration 186, 100%/100%): confirmed "Create SAP Notification"
button fully removed from DOM, "Post GRN in SAP" label correct, Warehouse select fully removed,
`/approve-full-auto` no longer 400s for non-P8 sites (now reaches the shipment-lookup 404 instead,
proving the allowlist check is gone), `finalize_goods_receipt` code-reviewed and confirmed to skip
movement correctly when `skip_movement=True`. No regressions to Reject/Mark Discrepancy/other
buttons. Minor non-blocking cosmetic note: confirm dialog's Site row could show blank if no site
selected yet - not fixed, low priority, left as-is.
- The Goods Movement lock/idempotency/escalation code from earlier today (see prior 3 session
  entries above) is UNCHANGED and still fully in place in `supplier_shipment_service.py` - it's
  just not currently being triggered by new GRN approvals anymore, since Put Away/movement is
  skipped by design now. It would need to be explicitly re-wired (remove `skip_movement=True`,
  restore the `_auto_finish_full_auto_grn` scheduling) if/when warehouse automation is turned
  back on for this flow in a future session.

## Stable checkpoint (Sep 19 2026, user's explicit instruction: "remember this state of the app throughout except STO as per last deployed version")
Everything below is confirmed intact/current EXCEPT the STO Goods Issue code, which must stay exactly as last deployed (see investigation below - the `try_post_goods_issue()` experiment was reverted, not kept):
- GRN qty-discrepancy gating (`GrnApprovalPage.jsx` `hasQtyDiscrepancy`, `grn-qty-mismatch-warning` banner) - live-tested, KEEP.
- "Pull Latest POs" manual refresh buttons on Supplier Dashboard + Admin Act-as-Supplier page - KEEP.
- 4-hour background full PO refresh loop (`start_supplier_po_full_refresh_loop`, `server.py`) - KEEP.
- Fast delta-pull PO refresh endpoints (`/supplier-portal/purchase-orders/refresh`, `/admin/act-as-supplier/{account_id}/purchase-orders/refresh`) - KEEP.
- PO 29735 out-of-order watermark safety margin fix (`RECENT_WINDOW_SAFETY_MARGIN_IDS`, `sap_po_client.py`) - KEEP.
- "Act as Supplier" only shows 4 accounts (2 real vendor codes RAD-P2-S/H1330 + 1 duplicate H1330 signup + 1 dummy S9999) - CONFIRMED NOT A BUG, this is real data (Supplier Portal signup+approval flow only, separate from full SAP vendor master, no display limit found in code - frontend caps at 8 when unsearched, backend has no limit at all).
- **STO**: `try_post_goods_issue()` in `stock_transfer_service.py` is back to EXACTLY the deployed baseline (releases first delivery found immediately, no full-coverage wait) - the investigation below's code fix was tried live then explicitly reverted per user's ask. Do not re-apply that fix without the user's explicit go-ahead.

## STO Inbound Receiving re-investigation (Sep 19 2026, re-confirmed DEAD via KBA 2691388 hypothesis)
User challenged the "definitively dead" conclusion with SAP KBA 2691388 (direct PGR needs receiving site's "Standard Receiving, Without Tasks" Logistics Model to be active+Consistent). Re-tested live and specifically:
- Confirmed P8 already has BOTH `EM1` (task-based, created 18-Sep-2026, used for vendor GRN Site Logistics Task automation - don't touch) AND `REC_P8` (Standard Receiving, Without Tasks: Yes, Consistent, since 26-Apr-2023) - exactly the config KBA 2691388 says should support direct PGR.
- Created a brand-new STO (STO-000123, order 32514, P1->P8, IRON-SCR 5 KGM), posted real GI (Outbound Delivery P1D1-560), then called `InboundDeliveryRelease` then `InboundDeliveryPGRBackground` directly on the fresh Inbound Delivery Notification (same ID, 1:1 mapping re-confirmed) before any UI touch.
- Result: **identical "action is disabled" on BOTH calls**, even with the correct no-task model present and "Check Consistency" reporting clean. Rules out "missing/wrong Logistics Model" as the cause.
- Conclusion still DEAD, but now with a fuller, evidence-backed ticket - see `/app/memory/SAP_SUPPORT_TICKET_DRAFT_InboundPGR.md` (rewritten this session with both KBA 3583076 + 2691388 and cross-referencing today's outbound-side MDRO/Execute-disabled finding as likely the same root tenant-level gap).
- **Leftover real-world state**: STO-000123 has GI posted (P1D1-560) but is NOT yet received at P8 - 5 KGM IRON-SCR sitting un-received (low-value scrap, but flagging so it doesn't get lost - needs a manual PGR in SAP UI, or let the normal Playwright-based `inbound_receipt_service.py` flow pick it up whenever that job next runs for this STO).

## Multi-line STO Goods Issue automation investigation (Sep 19 2026, closed - reverted to deployed baseline)

**User's ask**: fix multi-line STOs ending up with one Outbound Delivery PER LINE instead of one combined delivery, using only standard SOAP/OData (no Playwright/ABSL/Cloud Applications Studio), fully automated.

**What was tried, in order**:
1. Code-only fix in `try_post_goods_issue()` (`stock_transfer_service.py`): wait until the full requested quantity is covered by whatever delivery(ies) Analytics shows before calling Release, instead of releasing the first partial delivery seen. Logically sound, no SAP config dependency - but **could not be verified live** because none of 4 live test STOs (STO-000119/120/121/122, real orders 32482/32503/32489/32497) ever got a delivery from SAP within the 20-min poll window during the parallel Logistics Model experiment below.
2. SAP-side task-based Logistics Model experiment (site P8): user configured `P8_SHIPEM` (Standard Shipping, Automatic Generation of Tasks: Yes, Release Outbound Delivery: Automatically), found + deleted the old incumbent `SHI_P8` (Without Tasks) since ByD only picks one Standard Shipping model per site. Result: zero Site Logistics Tasks ever generated for any test order (checked live via `QuerySiteLogisticsTaskIn`).
3. Root cause found: creating/scheduling an **Outbound Delivery Run** (MDRO, doc type 833) failed in SAP UI with "MDRO instance is not active".
4. Confirmed via API too: found the real BO (`RequestOutboundDeliveryExecutionRun`, MDRO type 833), user published it as a live test Custom OData service (`deliveryrunem`), we called its `Execute` action directly on the existing Run (`RDOCWT1`) - **SAP rejected it: "Action ... not possible; action is disabled"**. Same root cause as #3 via a second independent path - task-based Mass Data Run processing for Outbound Delivery is disabled at the tenant/Business Configuration level, not fixable via any API this app has access to. Needs SAP Basis/implementation partner to scope it in, or an SAP support ticket.
5. Tried OData `$batch` (one changeset, all 3 lines' PGIInBackground with TaskBasedIndicator=true) as a non-task-based alternative - **also FAILED**: this tenant's custom OData service (`odataoutboundemergent`) rejects `$batch` outright with a generic 400 even for a trivial single-GET batch, meaning `$batch` itself isn't implemented by this service (not a payload bug).

**Outcome, per explicit user instruction ("keep it how it was before we started this adventure")**:
- The `try_post_goods_issue()` code fix (#1 above) was **reverted** back to the exact deployed baseline (releases the first delivery found, no full-coverage wait) - user wants to stick to the currently-deployed behavior since the fix was never live-verified.
- SAP-side config (deleted `SHI_P8`, created `P8_SHIPEM`, created Outbound Delivery Run `RDOCWT1`, created test Custom OData service `deliveryrunem`) was **NOT reverted yet** - still pending. **P8 currently has NO no-task Logistics Model** - `SHI_P8` was deleted, only the (non-functional, since MDRO is disabled tenant-wide) `P8_SHIPEM` remains. This needs a no-task replacement model recreated for P8 before any real STO ships through there again with normal (non-task) behavior restored.
- **P0 for next session**: recreate a `SHI_P8`-equivalent (Standard Shipping, Without Tasks: Yes, Release: Manually) for site P8 to restore normal shipping - or confirm P8_SHIPEM's current (task-based, but non-functional since MDRO disabled) settings don't actually block normal shipping in practice first.

## Part 6 (Sep 22 2026, same day) - STO Inbound GR pivoted to a STRICTLY SYNCHRONOUS engine, webhook/sweep REMOVED entirely
User rejected the Part 5 webhook + 20-min safety-sweep architecture outright (SAP's own 2-8min
Warehouse Order creation lag was being masked by async wait, not surfaced) - mandated a
transaction-driven chain with **zero polling, zero webhooks, zero background jobs**. Confirmed via
the user-uploaded `SiteLogisticsRequest` BO doc that `ReleaseForExecution` genuinely exists on that
BO's root node ("releases all confirmation items... Site Logistics Order and Site Logistics Lot are
created and released") - but it fires internally as part of the Inbound Delivery's own `Release`
action, not as a separately-callable step from our side.
- **Removed entirely**: `/api/webhooks/sap-put-away` route, `_verify_sap_webhook_auth`,
  `SAP_WEBHOOK_EVENTS_COLLECTION`, `EXTERNAL_WEBHOOK_PATH_PREFIXES` auth bypass, the 20-min
  `start_awaiting_sap_receipt_timeout_loop` sweep, and `complete_automated_receipt_for_lot`. No SAP
  Event Notification subscription is used anymore (still exists SAP-side, just nothing listens).
- **New engine** (`inbound_receipt_service.start_automated_receipt`), one pass per delivery, no
  retries anywhere in it: Acknowledge -> (quantity overrides if any) -> Release -> check
  `DeliveryProcessingStatusCode`: if Finished, done (non-task site, GR already posted by Release).
  Else, ONE immediate `find_recent_lots` lookup (new `sap_inbound_delivery_execution_client` method,
  no ObjectID filter, matches by site+product-set in Python) -> if found, `ConfirmAsPlanned` each
  activity, done. Else, one direct `PostGoodsReceipt` attempt as a last resort -> if that also fails,
  **raises immediately** ("SAP hasn't finished processing... please retry in a moment") - the whole
  receive request fails (400/job status "failed"), user retries manually a moment later.
- `post_inbound_receipt` (server.py) now calls this directly - no more "awaiting_sap" job phase.
  The existing job_id/polling scaffold is UNCHANGED and NOT what the user was rejecting (that's just
  a normal fire-and-forget request pattern for the UI spinner, resolves in seconds now, not minutes).
- Bug found+fixed during live testing: `find_recent_lots`'s first version tried
  `$orderby=SystemAdministrativeData/CreationDateTime desc` - this custom OData service doesn't
  expose that property (`HTTP 400: Property SystemAdministrativeData not found`), silently returning
  zero real candidates. Fixed by dropping `$orderby` entirely (relies on SAP's default order + a
  generous `$top=50`).
- **Live-tested against 2 real pending STOs** (results shared with user in chat before this write-up):
  - STO-000111 (P1, task-based, already Released from earlier testing) - Acknowledge/Release
    correctly no-op'd ("action is disabled"), refreshed status was Finished, real relocation posted
    (GAC 282822/282823, P1-HOLD -> P1-RM). Full end-to-end success via the new engine.
  - STO-000123 (P1->P8, IRON-SCR, the exact Sep 19 2026 "leftover real-world state" test order noted
    above) - Release/PostGoodsReceipt both "action is disabled" (matches the documented KBA 3583076
    dead-end, not a new bug), no Lot found (P8 genuinely runs "Standard Receiving, Without Tasks" -
    REC_P8 - so no Lot will ever exist for it). Engine correctly failed fast with a clear message
    instead of hanging/polling - this is a pre-existing, already-documented dead API path for this
    specific old STO, needs a manual PGR in SAP UI (unchanged from the Sep 19 note).
- Not yet run through `testing_agent` - self-tested via 2 real live SAP receive attempts + log
  inspection per user's explicit ask ("test via code and share reply in chat, then I'll test").
  User plans to verify the rest directly in SAP.

## Part 7 (Sep 22 2026, same day, MAJOR CORRECTION) - direct PostGoodsReceipt works with NO Release, invalidates the Lot/task-based assumption
User challenged Part 6's test methodology directly: Release was always called before PostGoodsReceipt in every attempt, so "action is disabled" never actually proved PGRBackground itself is blocked - it could just be an artifact of that ordering. User specified 2 controlled test protocols (Path A: Acknowledge -> PostGoodsReceipt, NEVER Release; Path B: SiteLogisticsRequest.ReleaseForExecution -> Lot -> ConfirmAsPlanned) and required fresh, never-touched STOs only.
- Created 2 brand-new STOs from scratch (STO-000138 P8->P1 09200726-01 1 EA order 32831, STO-000139 P1->P8 IRON-SCR 1 KGM order 32833) - required the user to manually complete Goods Issue in SAP UI first (this app's Goods Issue architecture is intentionally manual, see decision above - no way to automate this step).
- Captured full baselines before touching either: raw ItemQuantity rows (Quantity/UnitCode/QuantityRoleCode=18/QuantityOriginCode=4/QuantityTypeCode/ParentObjectID all present and normal), delivery status codes all `1` (virgin - Not Released/Not Started/Not Acknowledged), and real destination stock (P8-HOLD IRON-SCR 3.2 KGM, P1-HOLD 09200726-01 1.0 EA).
- **Path A test result - WORKS, no Release needed at all**: `Acknowledge` -> `PostGoodsReceipt` directly (skipping Release entirely) succeeded with HTTP 200 on BOTH deliveries (P1D1-572 receiving at P8, AND P8D1-261 receiving at P1 - the exact site previously assumed to be task-based). SAP itself flipped `ReleaseStatusCode` to `3` (Released) and `DeliveryProcessingStatusCode` to `3` (Finished) as a side effect. **Real inventory receipt independently confirmed via SAPInventoryClient**: P8-HOLD IRON-SCR went 3.2 -> 4.2 (+1 KGM exactly), P1-HOLD 09200726-01 went 1.0 -> 2.0 (+1 EA exactly) - not just a status flip, genuine physical stock landed. Never reached Path B at all - direct PGR succeeded both times, so the Lot/Warehouse Order/ReleaseForExecution route was never needed.
- **Root cause of every prior "action is disabled" result** (Part 5/6, and the Sep 19 2026 KBA 2691388 re-investigation): calling `Release` BEFORE `PostGoodsReceipt` is what disables PGRBackground for THIS tenant - not a blanket "PGRBackground is dead" tenant-wide rule. `KBA 3583076` genuinely reproduces, but only for a delivery that's ALREADY been Released without an immediate GR (a real, narrower dead-end state - exactly what STO-000123/P1D1-560 is stuck in from much older testing, unrelated to this fix).
- **Code fix** (`inbound_receipt_service.start_automated_receipt`): reordered to make direct `PostGoodsReceipt` (no Release) the PRIMARY path for every delivery. The old Release -> Lot-lookup -> ConfirmAsPlanned route is now only a FALLBACK, tried only if the direct PGR attempt itself gets rejected. This also means the whole SiteLogisticsLot/`find_recent_lots`/ConfirmAsPlanned machinery built in Part 5/6 is very likely dead weight going forward (kept only as a fallback for already-broken deliveries like STO-000123) - not removed yet since it's an unused-but-harmless fallback, not proven completely unreachable.
- Both test STOs (000138, 000139) completed fully end-to-end through the real API afterward (Acknowledge/PGR already done manually above, then the app's normal `/receive` endpoint ran the relocation step) - `receipt_status: "received"` on both, real GAC IDs (282800, 282828).
- A 3rd fresh STO (000140, order 32818) was created to verify the rewritten code through the actual endpoint end-to-end, but was abandoned before Goods Issue completed (removed from app DB, order 32818 still exists in real SAP awaiting manual GI if the user wants to complete or ignore/cancel it) - the rewrite itself is a straightforward reordering of the exact same calls already proven live above, not new untested logic.
- Not yet run through `testing_agent`.

## Part 8 (Sep 22 2026, same day) - parallelized per-line relocation calls for multi-line receipts
User's ask: can a 10-line STO receipt be safely brought under 10s? Relocation (`_relocate_receipt_from_hold`)
runs one Goods Movement SOAP call per line - previously sequential.
- `inbound_receipt_service._relocate_receipt_from_hold` now fires all lines' `_trigger_goods_movement` calls
  concurrently via a `ThreadPoolExecutor(max_workers=SAP_MAX_CONCURRENT_REQUESTS)` (same shared `sap_semaphore`
  cap of 3 in-flight SAP calls tenant-wide still applies - this only removes the function's OWN artificial
  serialization on top of that cap, doesn't raise the real limit). Result order preserved (zipped back to
  original item order).
- Not yet run through `testing_agent`.

## Part 9 (Sep 22 2026, same day) - Option 2 (SOAP batching) definitively RULED OUT, live-proven atomic
Live-tested sending 2 `GoodsAndActivityConfirmation` blocks in ONE SOAP call (1 valid 1 EA move + 1
deliberately-invalid 999999 EA move, same product/site) directly against production. **Result: the
ENTIRE call failed as a SOAP Fault (HTTP 500)** - the valid confirmation was rejected too, purely
because it was bundled with the invalid one. Verified via SAPInventoryClient: no partial posting
leaked (clean rollback, no corruption), but proves this service treats the whole request as ONE
atomic transaction - no per-item independence, no safeguard can fix this (it's a platform behavior,
not a parsing/attribution problem). Batching would make failures WORSE than today (1 bad line would
fail ALL lines in a batch, vs today's independent per-line success).
Possible-but-unbuilt alternative: a NEW custom OData service (via SAP OData Service Explorer, same
mechanism as the existing `khinbounddeliveryexecution`) exposing a Create-capable goods-movement
entity, since OData's own `$batch` mechanism supports independent per-changeset commits (the right
primitive for this) - unlike the SOAP service tested here. Unverified, unbuilt, would need the SAP
admin to expose it and then live-testing to confirm changeset independence actually holds.
**Current recommendation**: implement Option 1 (HTTP connection/session reuse in
sap_goods_movement_client.py etc - NOT YET DONE, safe, ~5-15% estimated gain) as the next step;
do not pursue SOAP batching. Full detailed trail (raw XML, exact test STOs, timings, root-cause
math for the ~29-30s/8-line observation) recorded in `/app/memory/sto_receipt_performance_
investigation.md` per user's explicit ask to document everything.




**User's explicit ask**: disable "Post GRN in SAP" if entered Actual Qty != Ship Qty; user confirmed
ANY mismatch (over OR under) should block posting, not just shortages.

**Changes**:
- `GrnApprovalPage.jsx`: new `hasQtyDiscrepancy` boolean (right after `isActionable`) - iterates
  `shipment.items`, compares each item's effective Actual Qty (`actualQtys[key] ?? it.actual_qty ??
  it.ship_qty`) against `ship_qty` with a `1e-6` float tolerance. Passed to the Post GRN button's
  `disabled` + `title` props, plus a new warning banner (`data-testid="grn-qty-mismatch-warning"`)
  rendered below the button row telling the user to click "Mark Discrepancy" instead. "Mark
  Discrepancy" button itself is untouched/still always enabled (the intended escape hatch).

**Tested via `testing_agent`** (iteration 187, 5/5 scenarios pass): default state (qty matches) ->
enabled; shortage -> disabled + warning + tooltip; overage -> disabled + warning + tooltip; restoring
qty -> re-enabled; Mark Discrepancy stays clickable throughout. No backend changes needed (purely
frontend gating - server-side posting endpoint itself was not touched).

**Env note found by testing_agent**: the live preview URL's backend/mongo is a DIFFERENT deployment
than this container's local `:8001`/mongo - local mongo seeding is NOT visible from the deployed
preview UI. Use `GET /api/admin/grn/shipments?status=in_transit` against the real preview URL to find
real pending shipments for any future UI testing, instead of seeding local mongo fixtures.
