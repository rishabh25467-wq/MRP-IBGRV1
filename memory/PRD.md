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
- Phase 5: External QMS feed

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
- Backend testing agent used this session (iteration_182): 10/10 pass on outbound
  generalization fix.
- Inbound generalization fix self-tested (pytest + direct mocked call-path check across
  P1/P2/P3/P8) - not yet run through testing_agent as a dedicated pass.
