# SAP BOM Lookup Tool

## Original Problem Statement
"build a new app to pull sap bom i need code file" — user provided an existing Python script (SAP Business ByDesign OData BOM extractor) as reference.

## User Choices
- Connect live to real SAP Business ByDesign credentials (not mock data)
- Display pulled BOM data in a table only (no export, no DB persistence)
- Search by a specific BOM ID (not bulk/all)

## Architecture
- Backend: FastAPI (`/app/backend/server.py`) + `sap_client.py` (SAP OData client, Basic Auth)
- Frontend: React single page (`/app/frontend/src/App.js`), Shadcn UI, IBM Plex Sans/Mono, Swiss high-contrast theme
- No database used — data fetched live from SAP per search, held only in frontend state
- No auth on the app itself

## SAP Connection
- SAP_INSTANCE_URL, SAP_USERNAME, SAP_PASSWORD stored in `/app/backend/.env`
- Uses two OData services: `bom_service` (hierarchy) and `bom_service1` (component details via EngineeringChangeOrderID filter, merged positionally with hierarchy items since ObjectIDs across the two services don't directly match)

## What's Implemented (2026-07-06 / Aug 6)
- `GET /api/bom/connection-status` — checks live SAP auth
- `GET /api/bom/search?bom_id=` — fetches hierarchy + component details, merges, returns groups/components with line_item, material_id, quantity, UOM, ECO, active status
- Frontend: search bar, SAP connection indicator, 4 stat cards (groups/components/active/last synced), results table, loading skeleton, error alert, empty state, sonner toasts
- Verified live against real tenant (my431827.businessbydesign.cloud.sap) with BOM `8060522_1` → 10 components / 2 groups
- Testing agent: 100% backend + frontend pass, no blocking issues. Fixed post-test: Line Item column now shows per-component sequence (10/20/30...) instead of duplicating group number.

## Bug Fix (2026-08-06)
- User reported BOM `FLT2_4.1` "does not show any items" — root cause: `get_component_details()` filtered bom_service1 by `EngineeringChangeOrderID eq bom_id` and zipped results positionally with hierarchy items; broke when sub-items reference a different ECO than the parent BOM (common in real/complex BOMs, e.g. FLT2_4.1's group 20 items reference ECO `FLT2_4.3`), silently dropping most items (2 shown instead of 16)
- Fix: replaced with direct ObjectID-key lookup — `change_state_object_id = item_object_id_int + 0x4000` (empirically verified constant offset against live SAP data), batched into chunked OData `$filter` queries, matched back by ObjectID instead of ECO name/position
- Verified: FLT2_4.1 now returns 16/16 components correctly; 8060522_1 regression-tested (still 10/10). Testing agent: 100% pass (9 backend tests, full E2E)
- Known limitation (not fixed, needs Material Master service not exposed in this tenant): some components show the parent product's own Material ID instead of a distinct raw-material ID, because SAP's `AssignedVariant` field reflects product variant assignment, not always the true component material. Full multi-level BOM explosion (matching native SAP "List of Production BOM" report with ~90 rows) would require recursive sub-BOM resolution — flagged as backlog, not yet built.

## Bug Fix #2 (2026-08-06): Full Multi-Level Explosion
- User needed the FULL multi-level BOM explosion matching SAP's native "Multi-Level BoM Visualization" report (not just Level-1 OData data). This required a NEW SOAP integration (`QueryProductionBillofMaterialsIn`) since OData couldn't provide real material IDs or recursion.
- User set up a SAP Communication Arrangement (guided step-by-step) generating SOAP endpoint + credentials (`SAP_SOAP_ENDPOINT`, `SAP_SOAP_USERNAME=_EMERGENTBOM`, `SAP_SOAP_PASSWORD`).
- Built `/app/backend/sap_soap_client.py`: recursive BFS explosion via `SelectionByProductionBillOfMaterialID` (root) + `SelectionByOutputProductID` (sub-assemblies), concurrent lookups (ThreadPoolExecutor), cycle detection, MAX_DEPTH=6.
- Bug: initial version returned 145 components for FLT2_4.1 (user expected ~91, matching their reference Excel). Root cause found via byte-level XML comparison against user's reference file: `SelectionByOutputProductID` returns MULTIPLE BOM revisions for the same product (old superseded + current); code was merging both instead of picking the current one. Fixed by picking the highest numeric revision suffix per hit, and skipping inactive/deleted items.
- Verified: FLT2_4.1 now returns exactly 91/91 components matching the reference file level-by-level (only 1 legitimate live-data difference remains — a part number updated in SAP since the reference was exported). Testing agent: 100% pass, full pytest + Playwright E2E.
- Removed unused `sap_client.py` (old OData-only module, fully superseded by SOAP).
- Known follow-up (not yet built): sub-BOM lookup failures (SAP timeouts) are silently swallowed with no retry — could cause a silently incomplete subtree on a slow SAP day.

## Bug Fix #3 (2026-08-06): Stale Revision Within Single Line Item
- User spotted a precision mismatch: level-1 "INSTRUCTION MANUAL" line showed stale product code `6902-602120`/ECO `FLT2_4.3` when SAP's live UI showed current `6902-602142`/ECO `FLT2_4.4`.
- Root cause: a single ItemGroupItem in the SOAP response can carry MULTIPLE `ProductionBillOfMaterialItemGroupChangeState` entries (revision history for that exact line); parser was grabbing the first (oldest) via `re.search` instead of the latest.
- Fixed: for each ItemGroupItem, now compares all ChangeState entries' `EngineeringChangeOrderID` revision suffix (float-parsed, handles both integer `_2` and decimal `_4.4` suffixes) and extracts fields from only the highest-revision one.
- Verified: FLT2_4.1 output now matches the reference Excel with ZERO discrepancies across all 5 levels/91 components. Testing agent: 100% pass, no regressions.

## Feature: Bare Part Number Auto-Resolution (2026-08-06)
- User wanted to search by bare part number (e.g. `P26584`) instead of needing to know the exact revision-suffixed BOM ID (e.g. `P26584_2`).
- `explode_bom()` now tries exact BOM ID match first, falling back to output-product resolution (reusing existing latest-revision-picking logic) if no exact match — fully backward compatible with exact-ID searches.
- Frontend shows a persistent "Resolved to: {bom_id}" badge so users can confirm which revision was found.
- Verified: bare `8060522` → resolves to `8060522_1` (23 components); bare `6800-004061` → resolves to `6800-004061_2` (latest of 2 revisions, not stale `_1`). Testing agent: 100% pass, exact-ID search and all prior regressions intact.
- Also fixed: intermittent component-count flakiness (91 vs 89) caused by transient SAP SOAP timeouts silently dropping subtrees — added 3-attempt retry with backoff to sub-BOM lookups. Verified stable across 7+ consecutive runs.

## Feature: Drillable Tree View (2026-08-06)
- User needed a hierarchical, collapsible view instead of a flat table — hard to tell which Level-2 item belongs to which Level-1 parent.
- Backend `explode_bom()` restructured to return a proper nested `tree` (each node has a `children` array) instead of a flat `rows` list, built during the same concurrent BFS resolution for speed.
- Frontend: default "drill down" mode shows only Level-1 rows collapsed; click a chevron to expand/collapse a branch; "Expand All"/"Collapse All" buttons control the whole tree at once; fresh search resets to collapsed. Rendering uses a flattened-visible-rows helper (not a recursive JSX component) to avoid a babel/visual-edits plugin crash on self-referencing components.
- Verified: testing agent 100% pass (12/12 backend, all frontend flows) — data correctness (91/5 for FLT2_4.1, 23/2 for 8060522_1) unaffected by the UI-only restructuring.

## Feature: Excel Export (2026-08-06)
- Added "Export to Excel" button (client-side, `xlsx`/SheetJS library) that downloads the FULL BOM tree (not just visible/expanded rows) as `BOM_{bom_id}.xlsx` with columns Level, Product ID, Description, Quantity, UOM, ECO, Active in DFS parent-then-children order.
- No backend changes — reuses already-fetched tree data in browser memory, no extra SAP calls.
- Verified: testing agent 100% pass — correct filenames, full row counts (23 for 8060522_1, 91 for FLT2_4.1) regardless of UI collapse state, button correctly hidden until a search succeeds.

## Backlog / Next Tasks
- P1: Excel/CSV export of search results
- P1: Bulk/all-BOMs pull mode
- P2: Persist search history in MongoDB
- P2: Material Master lookup to resolve MaterialUUID → readable Material ID when InternalID is absent
- P2: Multi-level (nested L3/L4/L5) BOM hierarchy support
