# Stock Transfer Order (STO) - Consolidated Context

Single reference for everything STO/Outbound-Delivery/Goods-Issue related. PRD.md has the same info spread
across dated session entries - this file exists so a future session doesn't have to hunt through it.

## Architecture (current, as of Sep 18 2026)

- **Creation**: `sap_sto_client.py` (SOAP `ManageCustomerRequirementIn`) creates the Stock Transfer Order in
  SAP - always Check before Maintain.
- **Goods Issue / Delivery**: `sap_outbound_delivery_client.py` (custom OData service `odataoutboundemergent`)
  - `try_post_goods_issue` in `stock_transfer_service.py` polls SAP for the Outbound Delivery Request, then:
    1. Checks Analytics (`sap_outbound_delivery_analytics_client.py`) for a finished/matched delivery.
    2. If a Delivery exists but isn't finished, automatically calls `release_outbound_delivery`
       (`OutboundDeliveryRelease` action) - this is the "automatic Release" added Sep 18 2026.
    3. If no Delivery exists yet at all, sets `gi_status = "awaiting_manual_gi"` - staff must click "Save" on
       SAP's own Delivery Proposal screen (no fields/quantities needed) to create it; the app then detects it
       and auto-releases.
  - Single-line and multi-line STOs are UNIFIED into this one flow (no more separate PGIInBackground-only path
    for single-line - that call never wrote the 6 GST/compliance fields anyway, so unifying didn't lose real
    capability).

## The one permanently-manual step

SAP's "Save" click (Delivery Proposal -> real Delivery, combining however many line items) has **no external
API equivalent** in this tenant. 9 documented, live-tested attempts across 2 sessions, all confirmed dead ends:

1. `PGIInBackground` grouped by ParentObjectID - "object does not exist" (item-scoped only).
2. `SLRequestDeliveryExecution` - malformed URI / needs a pre-existing `TargetSiteLogisticsRequestUUID` this
   app has no service to create.
3. `OutboundDeliveryRequestAllocate` - "Project outbound delivery request reference missing" (wrong scenario).
4. `CompleteDeliveryRequestedIndicator` at STO creation - combines the Request, but PGIInBackground still
   creates separate Deliveries per item.
5. `AutoReleaseOutboundDelivery=false` + explicit release - doesn't combine either.
6. `SLRequestDeliveryExecution` with a real UUID - "Site logistics request does not exist".
7. `SLPGIInBackground` (schedule-line scoped) - succeeds with no error but still produces N separate
   Deliveries for N lines.
8. Site Logistics Task (`sap_site_logistics_client.py`, `QuerySiteLogisticsTaskIn`/`ManageSiteLogisticsTaskIn`)
   - live-verified Sep 18 2026: this tenant's Site Logistics Tasks are only ever created for a production
     material-issue/receipt scenario (`OperationTypeCode=30`), zero hits for 4 real STO order IDs tried.
9. `StockTransferProposalRequest` (custom OData FunctionImport, discovered via fresh `$metadata` scan) -
   declared in the schema but returns 404 at runtime - never actually implemented server-side.

**Conclusion**: closing this gap needs either a new SAP Basis-provisioned service (checked: the standard
`II_MANAGE_OUTBOUND_DELIVERY_IN` only supports Update/Release/Read/UndoRelease on an ALREADY-EXISTING delivery,
so it would NOT help) or genuine custom ABSL development in SAP Cloud Applications Studio (needs a PDI
developer key + someone who can write ABSL - real dev work, not a config toggle). User has accepted the one
manual "Save" click as permanent for now.

## Manual Goods Issue flow (replaces old Playwright automation)

- Playwright automation for outbound GI (`sap_playwright_outbound_gi_service.py`) is **dormant/superseded**,
  kept in the codebase per user's explicit ask but never invoked by any live flow.
- Staff complete the one "Save" click directly in SAP; the app verifies + auto-releases via API afterward.
- `POST /api/stock-transfer/orders/{sto_id}/complete-manual-gi` - manual verification endpoint.

## GST / E-way bill compliance fields (6 fields)

- Transportation Mode, Vehicle No., Place Of Supply, G.R No., Date Of Supply, Freight Forwarder.
- **Never written to SAP** - confirmed hard-locked against API writes once SAP's scheduler picks up the order
  (verified 3 separate ways in an earlier session).
- **Mandatory + editable** in the app's own STO creation form (reverted Sep 18 2026 after briefly being made
  optional during the architecture shift - user's explicit follow-up ask).
- Pushed to the legacy ERP portal (`erp_portal_client.py`, `Pro_DeliveryChallan_Insert`) for the 4 fields that
  have a matching proc parameter: Vehicle No./G.R No./Date Of Supply/Freight Forwarder ->
  `veh_no`/`gr_no`/`gr_date`/`trans`. Transportation Mode and Place Of Supply have NO corresponding ERP proc
  parameter - they stay app-only by design, nothing more to wire there.
- Remark field: optional, free text.

## Bugs fixed this session (Sep 18 2026)

1. **Available Qty vs dropdown mismatch after Refresh** (`StockTransferPage.js`, `refreshItemStock`): the
   per-item "Refresh" button updated the warehouse dropdown's `locations` array but never re-synced
   `available_qty` (the column) for the already-selected warehouse. Fixed by re-looking-up the selected
   warehouse in the freshly refreshed locations.
2. **Detail modal stuck on "ERP Portal: syncing..."** (`StockTransferPage.js`): the list's own auto-poll gate
   (`hasRunningGiJob`) only watched `gi_job_running`, which flips false the instant GI posts - exactly when
   the separate ERP sync job starts. Fixed by also polling while `gi_status === "posted"` but
   `erp_portal_status` isn't yet terminal (`synced`/`failed`).
3. **Detail modal still not updating live while open** (follow-up): the cross-sync from `recentOrders` was too
   indirect. Added a direct `useEffect` that polls `GET /stock-transfer/orders/{sto_id}` every 4s while the
   modal is open, pushing fresh data into both `selectedOrder` and the matching `recentOrders` row.

## Key files

- `/app/backend/stock_transfer_service.py` - orchestration (`create_stock_transfer_order`,
  `try_post_goods_issue`, validation).
- `/app/backend/sap_sto_client.py` - STO creation (SOAP).
- `/app/backend/sap_outbound_delivery_client.py` - Goods Issue/Release (custom OData) - has the full 9-attempt
  history in its module docstring.
- `/app/backend/sap_outbound_delivery_analytics_client.py` - fast delivery-status read (Business Analytics).
- `/app/backend/sap_site_logistics_client.py` - dormant, confirmed not applicable (attempt #8).
- `/app/backend/erp_portal_client.py` - legacy MSSQL ERP portal sync (`Pro_DeliveryChallan_Insert`).
- `/app/backend/sap_playwright_outbound_gi_service.py` - dormant/superseded Playwright automation.
- `/app/frontend/src/pages/StockTransferPage.js` - creation form + recent orders list + detail modal.

## Public documentation

Every SAP integration this app uses (STO included) is now documented at the public, no-login page
`/docs/sap-integrations` (backed by `sap_integration_docs.py` + `GET /api/public/sap-integrations`).
