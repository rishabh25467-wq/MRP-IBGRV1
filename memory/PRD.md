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
