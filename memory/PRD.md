# PRD - SAP BOM Viewer / Materials Hub

## Original problem statement
Extend the existing SAP BOM viewer application: Production Plan page (OMS Open-PO Demand feed), 2-Tier MRP, Microsoft Entra ID (Azure AD) SSO, Supplier Portal for external vendors, PO Creation automation, Inbound STO Receipt page (groups multiple SAP delivery lines), native SAP Business ByDesign Custom Business Object (ABSL) replacing Playwright for multi-line STO Outbound Deliveries. Most recent asks (Sep 2026): Supplier Portal entity restriction + GRN Site/Warehouse auto-derivation + JDE theme visual pass, and a Production Confirmation bug (Net Weight wrongly mandatory for hardware-kit BOMs).

## Architecture
- `/app/backend/`: FastAPI + MongoDB. Key files: `server.py` (routes, incl. `_pick_rm_item`/`_compute_scrap_calc` scrap-weight logic), `supplier_shipment_service.py` (vendor shipment lifecycle), `sap_po_client.py` (PO cache/buyer entity), `sap_wip_clearing_client.py` (site->company RI/RT mapping), `sap_valuation_client.py` (pricing - has an open bug, see backlog).
- `/app/frontend/src/`: React. `GrnApprovalPage.jsx` (internal GRN approval), `pages/supplier-portal/*` (vendor-facing: Dashboard, Shipments, Login, Signup, Pending).
- Theme: JDE Enterprise ERP (`/app/design_guidelines.json`) - Chivo headings, IBM Plex Sans body, JetBrains Mono data, #004B87 primary, #0E7C86 header teal, #EAECF0 table headers, grid-bordered zebra tables. `tailwind.config.js` defines `fontFamily.heading/sans/data`.

## What's been implemented (Sep 2, 2026 session)
- **Supplier Portal entity restriction**: vendor must pick RI or RT (buyer entity) before seeing/acting on POs; entity switcher pills + forced picker card when 2+ entities exist; cart clears on switch. `SupplierDashboardPage.jsx`.
- **Shipment single-entity enforcement**: `supplier_shipment_service._resolve_items()` raises `ShipmentValidationError` if a shipment (create or edit) would mix RI+RT items; `SupplierShipmentsPage.jsx`'s edit "add item" search only offers same-entity POs.
- **GRN Approval Site/Warehouse derivation**: `GET /admin/grn/lookup/{doc_code}` returns `buyer_code`, `buyer_entity_name`, `allowed_site_ids` (RI: P1/P8, RT: everything else known), and `site_access_blocked` (blocks Site/Warehouse/Approve with a red banner when the user has zero bound sites for that entity; Reject/Discrepancy stay usable). Warehouse auto-preselects a `-QC` suffixed warehouse when available. Backend also validates site_id server-side in `prepare_approval()` (400 if mismatched).
- **JDE theme visual refresh**: `GrnApprovalPage.jsx` + all 5 Supplier Portal pages now match the rest of Materials Hub (teal h-16 headers, #004B87 buttons, grid-bordered zebra tables, Chivo/IBM Plex Sans/JetBrains Mono typography - additive `tailwind.config.js` change, app-wide, no regression).
- Tested via `testing_agent` twice (iteration_140 found 2 bugs, both fixed; iteration_141 regression 100% pass, 3 cosmetic issues found+fixed same session). No real SAP production write was ever triggered during testing.
- **Production Confirmation "Net Weight mandatory" bug fix** (Sep 2 2026): `_pick_rm_item()` used to fall back to "whichever mass-tracked BOM component has the biggest quantity" whenever nothing matched a known raw-stock keyword - for a hardware-bag BOM (Wall Anchor 309,772 qty > Lag Bolt 160,540 qty, both just fasteners, no real sheet/coil/flat/rod/pipe input) this wrongly picked the Wall Anchor as "the raw material being formed" and demanded a Net Weight be configured before confirmation would proceed. Fixed by: (1) adding Rod/Pipe/Angle/Channel/Mesh/Ingot to the recognized raw-stock keyword list (validated against all 1121 real BOMs with mass components - correctly auto-classifies 115 genuine items like "MS Rod"/"MS Pipe"/"Angle 65x65x5mm" with zero false positives), (2) removing the blind "biggest quantity wins" fallback for anything that still doesn't match a keyword - it now only applies if that exact finished item already has a Net Weight configured in `component_master` (real precedent, e.g. ~9 legacy cryptic-coded items like "SUSPENSION SECTION" keep working exactly as before). Verified against real production data via a standalone script before shipping; user will self-test on their next real confirmation (no testing_agent run - user's choice).

## Prioritized backlog
### P0
- **Valuation Rate Mismatch on Print**: `sap_valuation_client.get_standard_costs()` picks a random site's price when start dates tie across sites - must filter by `site_id`'s `PermanentEstablishmentUUID`. NOT YET FIXED (deprioritized behind other work per user's explicit ordering).
- **External QMS feed**: secured API/data feed for external QMS app to pull pending-QC items. Not started.

### P1
- **Thread Pool Exhaustion**: `asyncio.to_thread` on blocking SAP calls exhausts the default executor, causing ~60s stalls under load. Move to a dedicated `ThreadPoolExecutor`. Not started (explicitly deferred by user).
- **SAP Custom BO / ABSL Web Service Authorization**: blocked on SAP Admin fixing Work Center View binding for `CombineView` in SAP UI Business Roles.
- **Supplier GRN Live Test**: blocked on SAP production tenant (`my431827...`) responsiveness.
- `StockTransferPage.jsx` refactor (~1400 lines), STO stuck-order nav badge, "last confirmed in SAP" timestamp on stale PO rows, auto-refresh a site's stock after its own STO posts, bulk-import diff preview, missing-weight alert, auto-retry ERP sync.
- `GrnApprovalPage.jsx` is ~610 lines mixing lookup/approve/reject/discrepancy/retry/pending-table - approaching split threshold per code review (iteration_141).

### P2
- e-Way Bill / e-Invoice via Sandbox.co.in - blocked on user providing API credentials.
- Delivery Note Batch Print, Fuzzy Product Match (AI Quick Entry), Bulk Finish reporting points, Category Picker dropdown.

### P3
- Backfill old CompCode on legacy ERP rows, incorporate Quality/OTIF into AI Quota Suggestions.

## Known constraints / learnings
- RI (RAY INTERNATIONAL) sites today: P1, P8 (also nominally P1W/W1 per SITE_TO_COMPANY map, but those have zero live inventory so never appear in `list_known_sites`). RT (RADISH TECHNOLOGIES) = everything else, including P2W.
- QC warehouse does not exist at every site - the "default to QC" behavior is a soft pre-select, never a hard requirement.
- Never trigger a real "Approve & Post to SAP" click/call on a real PO outside of a deliberate, user-approved live test - it queues a real Playwright job against production SAP.
- Scrap/by-product Net Weight enforcement (`_pick_rm_item` in server.py) must NEVER guess an RM by "biggest quantity" for an item with no prior Net Weight precedent - only known raw-stock keywords (Iron/CR/SS/Alu/Brass/Copper family incl. Rod/Pipe/Angle/Channel/Mesh/Ingot) or an already-configured `component_master.net_weight_kg` may trigger the requirement.
