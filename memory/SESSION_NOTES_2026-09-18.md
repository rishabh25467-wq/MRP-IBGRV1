# Session Notes - Sep 18 2026 (continuity checkpoint)

## Task 1: STO {SITE}-HOLD relocation generalization - DONE, TESTED
- Fixed blocking lint error in `stock_transfer_service.py` (stale `RELOCATION_SITE_ID` /
  `_relocate_items_to_p8_source_warehouse` refs).
- Generalized OUTBOUND pre-STO relocation (`stock_transfer_service.py`,
  `_relocate_items_to_source_hold_warehouse`) to run for ANY `ship_from_site_id`, not just P8.
- Generalized INBOUND post-receipt relocation (`inbound_receipt_service.py`,
  `_relocate_receipt_from_hold` / `_receipt_hold_warehouse_id`) to run for ANY
  `ship_to_site_id`, not just P8. This was the root cause of the user's original report:
  "STO P3->P2 background movement not completing" (ship_to=P2 never matched old P8-only
  check, stock stayed stuck in P2-HOLD after receipt).
- User explicitly confirmed (Sep 18): do NOT restore old single-line PGIInBackground Goods
  Issue automation - the Sep 18 manual-GI-for-all-STOs architecture decision stays as-is.
- Verified: testing_agent (10/10 pass, iteration_182), pytest, mocked call-path checks
  across P1/P2/P3/P8. One pre-existing unrelated test failure
  (`test_relocate_returns_stock_not_exist_error_for_zero_hold_stock`) traced to an earlier
  commit `f0b2b06` (STO-000100 fix removed a local stock pre-check) - NOT caused by this
  session's changes, not fixed (out of scope, flagged in PRD.md).
- PRD.md already updated with this work.

## Task 2: ERP sync resume - PARTIALLY PLANNED, NOT YET EXECUTED
- Context: `STO_ERP_SYNC_PAUSED="true"` in `/app/backend/.env` makes
  `stock_transfer_service.sync_to_erp_portal()` skip the real legacy-ERP write and mark
  `erp_portal_status: "paused"` instead - was deliberately set so preview/test STOs don't
  create bogus Delivery Challans in the real ERP.
- User's decision: "1 and 3 only. Don't retry paused records" (from my 3-option plan:
  1=flip flag to false for new STOs, 2=retry stuck paused records, 3=account for ERP-side
  format/field changes).
  - Confirmed: flip `STO_ERP_SYNC_PAUSED` to "false" so sync resumes for NEW STOs going
    forward.
  - Confirmed: do NOT retry/resync existing STOs currently stuck at `erp_portal_status:
    "paused"`.
  - Item 3 ("what changed on ERP side") - asked user for details, conversation got
    redirected to Task 3 (GRN bug) before getting an answer. STILL NEED TO ASK/CONFIRM
    what ERP-side changes (if any) need accounting for before flipping the flag.
- STATUS: Flag has NOT been flipped yet. Waiting on:
  (a) answer to "what changed on ERP side" (item 3), AND
  (b) user's answer to my latest question: "should I flip STO_ERP_SYNC_PAUSED now, or hold
  off until the GRN bug (Task 3) is fully resolved?"
- NEXT ACTION: once both answered, flip `STO_ERP_SYNC_PAUSED` to `"false"` in
  `/app/backend/.env` via search_replace (single key only, never rewrite whole file), then
  restart backend (env change requires `sudo supervisorctl restart backend`), then verify
  with a real/test STO sync end-to-end (or ask user to verify live since this hits a real
  ERP system).

## Task 3: GRN S000001 warehouse-movement bug - CODE FIX DONE, AWAITING LIVE CONFIRMATION
- User-reported new bug (separate from Task 1/2): GRN S000001 (Confirmed GRNs tab) shows
  "Warehouse move pending" with 2 line-item errors in the movement popover:
  - IRON-SCR: "SAP rejected the movement: No inventory items found for external id
    MOV-82F4B8 item id I-670D0104"
  - 13INTIEBELT: HTTP 500 SOAP Fault - "You cannot carry out goods movements involving
    this logistics area. To be included in goods mo[vements]..." (fault text was cut off
    mid-sentence in the screenshot, never got the full remaining text)
- ROOT CAUSE (confirmed via 2 user-provided SAP screenshots):
  1. SAP's "Inbound Warehouse Order Overview" (Request ID 86150, PO 29703, Delivery 53606,
     Site P3) showed BOTH items' Target Logistics Area ID = "P3-HOLD" (with Inspection IDs
     72655/72656), NOT "P3-RM".
  2. SAP's "Stock Overview" for 13INTIEBELT confirmed 3 EA sitting at Site P3 / Logistics
     Area "P3-HOLD" with the "Inspection" checkbox checked (Quality Inspection status).
  3. `supplier_shipment_service.py`'s `_post_goods_movement_for_items` had the EXACT SAME
     P8-only hardcoding bug as Task 1: `source_area = "P8-HOLD" if site_id == "P8" else
     f"{site_id}-RM"` - for P3 this wrongly assumed source_area="P3-RM" (empty), so the
     stock-status lookup (`_resolve_source_stock_status`) found nothing there and the
     movement was attempted from the wrong/empty warehouse entirely.
- FIX APPLIED (this session, `/app/backend/supplier_shipment_service.py` ~line 907-925):
  changed `source_area = "P8-HOLD" if site_id == "P8" else f"{site_id}-RM"` to unconditional
  `source_area = f"{site_id}-HOLD"` for every site - mirrors the Task 1 fix pattern exactly.
- VERIFIED (self-test, mocked): `_post_goods_movement_for_items` for site P3 now correctly
  builds `source_logistics_area_id="P3-HOLD"` and `target_stock_status_code="1"`
  (Inspection - auto-detected by the pre-existing `_resolve_source_stock_status` helper,
  which was already generic/not P8-specific, it just needed the right warehouse to look
  in). pyflakes clean, backend healthy (200 on regression GET).
- NOT YET DONE: a REAL live retry against the actual SAP tenant for GRN S000001 to confirm
  both IRON-SCR and 13INTIEBELT actually post successfully now. This requires the user (or
  a live test) to click the existing retry-warehouse-move button on GRN S000001 in the
  Confirmed GRNs tab, since this is a real production SAP tenant and I should not trigger
  unnecessary live writes myself without being asked to.
  - Open question to user: is 13INTIEBELT's fault ("cannot carry out goods movements
    involving this logistics area") purely a symptom of the WRONG source area (now fixed),
    or a genuinely separate SAP logistics-area config/master-data restriction that still
    needs SAP Basis to fix? Won't know until a live retry is attempted.
- LAST MESSAGE SENT TO USER (awaiting response): asked (a) to retry GRN S000001's warehouse
  move live and report back whether both items now succeed, and (b) whether to flip
  `STO_ERP_SYNC_PAUSED` now or hold off until this GRN investigation is fully closed.

## Key file/line references for quick resume
- `/app/backend/stock_transfer_service.py`: `_relocation_hold_warehouse_id` (~585),
  `_relocate_items_to_source_hold_warehouse` (~589), called from `submit_order_to_sap`
  (~649).
- `/app/backend/inbound_receipt_service.py`: `_receipt_hold_warehouse_id` (~75-76),
  `_relocate_receipt_from_hold` (~79), `retry_receipt_relocation` (~125), called from
  `finalize_receipt` (~320).
- `/app/backend/supplier_shipment_service.py`: `_resolve_source_stock_status` (~821),
  `_post_goods_movement_for_items` (~867), `source_area` fix (~907-925). Retry entry point:
  `retry_goods_movement` (~1362).
- `/app/backend/.env`: `STO_ERP_SYNC_PAUSED="true"` (line ~19) - still true, not yet flipped.
- ERP sync logic: `/app/backend/stock_transfer_service.py::sync_to_erp_portal` (~1127),
  the pause check is at ~1200.

## Do NOT do (explicit user instructions this session)
- Do NOT restore single-line PGIInBackground automatic Goods Issue.
- Do NOT retry/resync existing STOs stuck at `erp_portal_status: "paused"` when flipping
  the ERP sync flag - only new STOs going forward.
- Do NOT trigger live SAP writes/retries myself without being asked (real production SAP
  tenant) - ask the user to do live retries and report back where possible.
