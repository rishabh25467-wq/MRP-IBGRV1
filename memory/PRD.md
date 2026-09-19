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

## Completed this session (Sep 2026 fork)
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

## Backlog

### P0
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

## Testing status
  generalization fix.
- Inbound generalization fix self-tested (pytest + direct mocked call-path check across
  P1/P2/P3/P8) - not yet run through testing_agent as a dedicated pass.

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
