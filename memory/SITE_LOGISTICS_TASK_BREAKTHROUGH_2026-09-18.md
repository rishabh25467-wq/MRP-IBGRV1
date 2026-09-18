# BREAKTHROUGH: Direct SOAP STO Outbound Automation via Site Logistics Task - Sep 18 2026

## INBOUND STO receiving via this same mechanism - DEFINITIVELY ANSWERED: NO
Re-investigated same day (user asked directly) whether the outbound Pick-task
SOAP breakthrough above also applies to INBOUND STO receiving (Putaway tasks).
Answer: NO, confirmed dead twice now - do not re-investigate this again:
1. Already tested once before, Aug 28 2026 (see sap_playwright_pgr_service.py's
   docstring) - 0 Site Logistics Task hits for inbound at the time.
2. Re-confirmed live today: the user's ACTUAL receiving process is a single
   "Post Goods Receipt As Planned" UI click - no task/warehouse-request step
   involved at all. The 3 real Putaway tasks found at P8 today (68750/68791/
   68792) are from an unrelated Production Order flow, not STO/vendor receiving.
3. The API equivalent of that exact button, `InboundDeliveryPGRBackground`
   (sap_inbound_delivery_client.py), is permanently disabled by SAP itself for
   this tenant - confirmed via SAP's own KBA 3583076. This is a genuine SAP-side
   platform limitation, not a code/schema issue we can fix from our side.
Decision (user's explicit call): accept manual receiving in SAP UI for now
(matches current reality). A SAP Support ticket has been drafted (see
`/app/memory/SAP_SUPPORT_TICKET_DRAFT_InboundPGR.md`) asking SAP to enable this
- the only path that could still unblock it, but depends on SAP, not on us.

## REVERTED same day, per user's explicit decision
User decided to deactivate EM2 and go back to the original pre-session workflow:
create STO in app -> manually click "Create Outbound Delivery" + Release in SAP UI
(single click, as it was before this investigation). All code from this file's
investigation was reverted: `sap_site_logistics_client.py` restored to its Sep 16
state (was never wired into production anyway), and the "extended GI retry" fix in
`stock_transfer_service.py`/`server.py` was undone. Kept below purely as a research
record in case this direction is revisited later (e.g. if EM2 gets reactivated).

## TL;DR
Confirming a Site Logistics "Pick" task via pure SOAP (`ManageSiteLogisticsTaskIn.MaintainBundle_V1`)
fully automates STO Goods Issue for sites where EM2 ("Standard Shipping with pick lists"
Logistics Model) is active - NO Playwright, NO manual SAP UI clicks. PROVEN LIVE on
STO-000111 (P8->P1): confirming Task 68769 auto-created AND auto-finished Outbound
Delivery **P8D1-239** for both line items in one shot.

## Why the earlier "CONFIRMED DEAD END" note in `sap_site_logistics_client.py` was WRONG
That note (from an Aug 27/Sep 16 investigation) queried site **P2**, which never had EM2
set up - every task there was `OperationTypeCode=30` (production-only), none STO-related.
Once EM2 (Standard Shipping with pick lists) was created for **P8** this session, P8 DOES
generate real `OperationTypeCode=21` Pick tasks tied to Stock Transfers - confirmed via
screenshots (Task 68769, Reference "32294 - Stock Transfer") AND the live SOAP query.
Lesson: a negative finding on one site does NOT generalize tenant-wide once site-specific
Logistics Model config changes.

## The two clients
- `sap_site_logistics_client.py::SAPSiteLogisticsQueryClient.find_tasks_for_site(site_id)`
  - Read-only, safe anytime. Schema fix applied THIS session: drop `UpperBoundarySiteID`
    (only `LowerBoundarySiteID`), add a `<ProcessingConditions>` sibling node before the
    selection element (`QueryHitsMaximumNumberValue`/`QueryHitsUnlimitedIndicator`) -
    without both of these SAP throws a generic, useless "An exception was raised" fault.
  - Returns one entry per Site Logistics Task, each with `task_uuid`,
    `referenced_object_uuid`, `operation_activity_uuid`. NOTE: for multi-line tasks, the
    raw XML actually contains MULTIPLE `MaterialInput`/`MaterialOutput` blocks nested
    inside ONE `SiteLogisticsLotOperationActivity` (one pair per STO line, each with its
    own `SiteLogisticsLotMaterialInputUUID`/`SiteLogisticsLotMaterialOutputUUID` and
    `LineItemID`) - the current `find_tasks_for_site` parser does NOT extract these nested
    per-line UUIDs (only the outer task-level ones). If you need to confirm a multi-line
    task WITHOUT already knowing the per-line UUIDs from elsewhere (we got them from a
    raw XML dump, not from the parsed helper), extend `_all_blocks`/add a dedicated parser
    for `MaterialInput`/`MaterialOutput` blocks first.

- `sap_site_logistics_client.py::SAPSiteLogisticsManageClient.confirm_tasks_bundle(tasks)`
  - REAL WRITE, irreversible. Schema CONFIRMED this session against the actual WSDL (user
    pulled it live from SAP Service Explorer - saved as a reference, ask user if needed
    again, was NOT already present among previously-uploaded WSDL assets).
  - Two bugs fixed this session that caused a generic undiagnosable "Web service
    processing error" 500 fault:
    1. A root-level `<BasicMessageHeader/>` element is REQUIRED (send it empty - its own
       children are all optional) even though it's easy to miss since it's not mentioned
       anywhere in the query-side schema.
    2. The maintain-request field names for input/output identifiers are
       `MaterialInputUUID`/`MaterialOutputUUID` - NOT the query-response's
       `SiteLogisticsLotMaterialInputUUID`/`SiteLogisticsLotMaterialOutputUUID`. Easy trap:
       these look like they should be the same field, they are not.
  - `tasks`: list of `{task_uuid, referenced_object_uuid, operation_activity_uuid, items:
    [{material_input_uuid, material_output_uuid, product_id, quantity, unit_code,
    source_warehouse_id, target_warehouse_id}]}`. One task can carry multiple `items`
    (one per STO line) - all confirmed in one call, matching the UI's multi-select-confirm
    behavior.
  - `target_warehouse_id` deliberately omitted (not sent as empty tag) when unknown - SAP
    determines it itself, matches what the SAP UI itself shows (blank) before confirmation.

## Live test (Sep 18 2026, real SAP tenant, real STO - NOT simulated)
- STO-000111 (P8->P1, sap_order_id 32294, gi_delivery_request_id 61098), items
  09200726-01 (qty 1 EA) + 092018-PF (qty 1 EA), both from P8-HOLD.
- Site Logistics Task 68769 (Pick, Not Started) held BOTH lines in one
  `SiteLogisticsLotOperationActivity` - `task_uuid=fa163eca-1723-1fd1-acdc-670a15ec4b79`,
  `operation_activity_uuid=fa163eca-1723-1fd1-acdc-670a15eaeb79`.
- `confirm_tasks_bundle` call: SAP returned `SiteLogisticsTaskSeverityCode=S`,
  `SiteLogisticsTaskNote="Saved Successfully"`.
- Immediately after: task 68769 disappeared from the open-task list (3 tasks left, was 4).
- `SAPOutboundDeliveryAnalyticsClient.find_deliveries_for_sto` (needs
  `SAP_ODATA_USERNAME`/`SAP_ODATA_PASSWORD`, NOT `SAP_SOAP_USERNAME`/`PASSWORD` - different
  credential pair, easy to mix up) confirmed: Outbound Delivery **P8D1-239** created,
  BOTH lines `status_code=3` ("Finished"), quantities exactly matching (1.0 EA each).
- Ran the app's real production function `stock_transfer_service.try_post_goods_issue`
  directly (not a one-off script) - it correctly detected this via the existing
  Analytics-based auto-detection path and set `gi_status: "posted"`,
  `outbound_delivery_ids: ["P8D1-239"]` on STO-000111. No new detection logic needed -
  the existing `try_post_goods_issue`/background poll already handles this once the
  delivery exists and is Finished.

## What this means / next steps
- This is a genuinely NEW automation path for STO outbound Goods Issue, on TOP of the
  existing Sep 18 "manual GI for all STOs" architecture decision - it does not contradict
  that decision (that decision was about NOT trying to force `PGIInBackground`
  single-line automation again; this is a completely different SAP mechanism/task-based
  flow that happens to fully automate the common case when it's available).
- ONLY works for sites with EM2 (Standard Shipping with pick lists) active - currently
  just **P8**. Other sites still need Playwright... wait, Playwright is stopped by policy
  - other sites currently have NO automation path for outbound GI until their own EM2
    equivalent is created (mirror the "How to fix per site" steps from
    `SOAP_GRN_BREAKTHROUGH_2026-09-18.md`'s EM1 section, but for Standard Shipping instead
    of Standard Receiving).
- STILL UNKNOWN: does the Pick Task always exist immediately after STO creation (ready to
  confirm right away), or does it need something else to trigger its generation first
  (recall EM2's template had "Automatic Generation of Tasks" UNCHECKED by default - user
  may need to verify whether tasks are auto-generated for every new STO on P8, or need a
  manual "Create Pick List" step first before our SOAP confirm can run against it).
- NOT yet wired into production code (`stock_transfer_service.py`) - this was proven via
  direct script calls only. Building this into the real STO flow (e.g. a background
  poller that queries `find_tasks_for_site` for pending Pick tasks matching a STO's
  `sap_order_id`/`gi_delivery_request_id` reference and auto-confirms them) is the logical
  next step, but needs the multi-line UUID extraction gap in `find_tasks_for_site` fixed
  first (see above) and should probably stay site-gated (P8 only) until other sites get
  EM2 too, mirroring the vendor-GRN feature-flag approach.
