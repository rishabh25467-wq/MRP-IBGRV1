# PRD - SAP BOM Viewer / Materials Hub

## Original problem statement
Extend the existing SAP BOM viewer application: Production Plan page (OMS Open-PO Demand feed), 2-Tier MRP, Microsoft Entra ID (Azure AD) SSO, Supplier Portal for external vendors, PO Creation automation, Inbound STO Receipt page (groups multiple SAP delivery lines), native SAP Business ByDesign Custom Business Object (ABSL) replacing Playwright for multi-line STO Outbound Deliveries. Most recent ask (Sep 2026, "mrp vendor side changes.docx" + a JDE-theme design ask): Supplier Portal entity restriction, GRN Approval Site/Warehouse auto-derivation, and a visual consistency pass.

## Architecture
- `/app/backend/`: FastAPI + MongoDB. Key files: `server.py` (routes), `supplier_shipment_service.py` (vendor shipment lifecycle), `sap_po_client.py` (PO cache/buyer entity), `sap_wip_clearing_client.py` (site->company RI/RT mapping), `sap_valuation_client.py` (pricing - has an open bug, see backlog).
- `/app/frontend/src/`: React. `GrnApprovalPage.jsx` (internal GRN approval), `pages/supplier-portal/*` (vendor-facing: Dashboard, Shipments, Login, Signup, Pending).
- Theme: JDE Enterprise ERP (`/app/design_guidelines.json`) - Chivo headings, IBM Plex Sans body, JetBrains Mono data, #004B87 primary, #0E7C86 header teal, #EAECF0 table headers, grid-bordered zebra tables. `tailwind.config.js` now defines `fontFamily.heading/sans/data`.

## What's been implemented (Sep 2, 2026 session)
- **Supplier Portal entity restriction**: vendor must pick RI or RT (buyer entity) before seeing/acting on POs; entity switcher pills + forced picker card when 2+ entities exist; cart clears on switch. `SupplierDashboardPage.jsx`.
- **Shipment single-entity enforcement**: `supplier_shipment_service._resolve_items()` now raises `ShipmentValidationError` if a shipment (create or edit) would mix RI+RT items; `SupplierShipmentsPage.jsx`'s edit "add item" search only offers same-entity POs.
- **GRN Approval Site/Warehouse derivation**: `GET /admin/grn/lookup/{doc_code}` returns `buyer_code`, `buyer_entity_name`, `allowed_site_ids` (RI: P1/P8, RT: everything else known - live from `inventory_service.list_known_sites` + `sap_wip_clearing_client.company_and_set_of_books_for_site`), and `site_access_blocked` (entity known but user has zero bound sites for it - blocks Site/Warehouse/Approve, shows red banner, Reject/Discrepancy stay usable). Warehouse auto-preselects a `-QC` suffixed warehouse when the site has one (still editable). Backend also validates site_id server-side in `prepare_approval()` (400 if mismatched).
- **JDE theme visual refresh**: `GrnApprovalPage.jsx` + all 5 Supplier Portal pages now match the rest of Materials Hub - teal h-16 headers, #004B87 buttons/links (was bright #0076CC), grid-bordered zebra tables with #EAECF0 headers, Chivo/IBM Plex Sans/JetBrains Mono typography (added to `tailwind.config.js`, app-wide, additive/no regression).
- **UX fixes from testing (iteration_141)**: Supplier edit-dialog Save button now disabled until every line has a valid qty > 0 (was surfacing raw 422 errors); GRN Warehouse placeholder no longer stuck on "Loading..." when no site chosen; shipment-created success dialog no longer visually overlaps the closing confirm dialog.
- Tested via `testing_agent` (iteration_140 found 2 bugs, both fixed; iteration_141 regression 100% pass, 3 low/medium cosmetic issues found and fixed same session). No real SAP production write was ever triggered during testing (verified via `sap_sync_status` and cleaned up all test fixtures/sessions/shipments).

## Prioritized backlog
### P0
- **Valuation Rate Mismatch on Print**: `sap_valuation_client.get_standard_costs()` picks a random site's price when start dates tie across sites - must filter by `site_id`'s `PermanentEstablishmentUUID`. NOT YET FIXED (diagnosed, deprioritized behind Supplier Portal work per user's explicit ordering).
- **External QMS feed**: secured API/data feed for external QMS app to pull pending-QC items. Not started.

### P1
- **Thread Pool Exhaustion**: `asyncio.to_thread` on blocking SAP calls exhausts the default executor, causing ~60s stalls under load. Move to a dedicated `ThreadPoolExecutor`. Not started (explicitly deferred by user this session).
- **SAP Custom BO / ABSL Web Service Authorization**: blocked on SAP Admin fixing Work Center View binding for `CombineView` in SAP UI Business Roles.
- **Supplier GRN Live Test**: blocked on SAP production tenant (`my431827...`) responsiveness.
- `StockTransferPage.jsx` refactor (~1400 lines), STO stuck-order nav badge, "last confirmed in SAP" timestamp on stale PO rows, auto-refresh a site's stock after its own STO posts, bulk-import diff preview, missing-weight alert, auto-retry ERP sync.
- `GrnApprovalPage.jsx` is 610 lines mixing lookup/approve/reject/discrepancy/retry/pending-table - approaching split threshold per code review (iteration_141).

### P2
- e-Way Bill / e-Invoice via Sandbox.co.in - blocked on user providing API credentials.
- Delivery Note Batch Print, Fuzzy Product Match (AI Quick Entry), Bulk Finish reporting points, Category Picker dropdown.

### P3
- Backfill old CompCode on legacy ERP rows, incorporate Quality/OTIF into AI Quota Suggestions.

## Known constraints / learnings
- RI (RAY INTERNATIONAL) sites today: P1, P8 (also nominally P1W/W1 per SITE_TO_COMPANY map, but those have zero live inventory so never appear in `list_known_sites`). RT (RADISH TECHNOLOGIES) = everything else, including P2W.
- QC warehouse does not exist at every site (e.g. missing at P4-family) - the "default to QC" behavior is a soft pre-select, never a hard requirement.
- Never trigger a real "Approve & Post to SAP" click/call on a real PO outside of a deliberate, user-approved live test - it queues a real Playwright job against production SAP.
