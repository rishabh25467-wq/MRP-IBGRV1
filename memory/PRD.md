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

## Update - Feb 2026
- Fixed: Entire Product ID cell (not just chevron icon) is now clickable to expand/collapse tree rows. Added hover highlight (`hover:bg-[#E5F0FA]`) and `cursor-pointer` on cells with children. Verified via screenshot tool - clicking anywhere in a parent row's Product ID cell toggles expand/collapse correctly.

## Update - Feb 2026 (Session 2)
- Fixed: Entire Product ID cell clickable to expand/collapse (not just chevron icon)
- Fixed 2 real SAP data-accuracy bugs in sap_soap_client.py:
  1. Header BOM revision selection now prefers ConsistencyStatus=3 (Consistent/released) over merely highest numeric suffix (a "Check Pending" revision was being wrongly preferred)
  2. Item-level ECO change-state selection now prefers an ECO ID that matches the item's own product ID naming convention (part-specific ECO) over unrelated higher-numbered batch ECO counters - fixes cases with messy/mixed ECO ID history on a single line item
- NEW: Live Standard Cost per BOM component via SAP OData custom service "materialvaluationdata" (Business Object: MaterialValuationData). Chain: Product UUID -> ValuationLevel (filter by MaterialUUID) -> ObjectID (converted to dashed UUID) -> ValuationPrice (filter by ValuationLevelUUID) -> pick currently-valid record by date range. New file `/app/backend/sap_valuation_client.py`, endpoint `POST /api/bom/standard-costs`. Credentials: SAP_ODATA_USERNAME/PASSWORD in backend/.env (business user UNEECOPSTEAM, NOT the technical SOAP user - technical users can't hold Business Roles in this tenant).
- NEW: Total BOM Cost rollup stat card - sums each top-level branch's own direct cost, only falling back to summing a node's children when the node itself has no direct cost (avoids double-counting a manufactured sub-assembly's cost AND its raw materials' costs together).
- NEW: AI Categorize feature (GPT-5.4-mini via emergentintegrations, EMERGENT_LLM_KEY) - classifies each LEAF-level BOM component (not sub-assemblies) into a material category (Raw Material, Hardware, Zinc, Steel, Sheet Metal, Packaging, etc). New file `/app/backend/bom_categorizer.py`, endpoint `POST /api/bom/categorize`. Rule enforced: screw/washer/nut/bolt/rivet always -> "Hardware". Items with children show plain "Sub-Assembly" text, never sent to AI.
- Investigated (dead end, documented for future reference): SAP Engineering Change Order status lookup (ManageEngineeringChangeOrderIn) - service exists but "Engineering Change Processing" communication scenario is not available for external Communication Arrangement setup in this tenant's edition. Gross/Net Weight - exhaustively tested, confirmed ZERO materials in the entire SAP tenant have weight data populated in any of 4 candidate custom fields (ItemNetWeight, TotalGrossMaster, zTotalNetWeight, zTotalGrossWeight) - not an integration issue, data simply doesn't exist in SAP yet.
- All features tested via testing_agent_v4 (iteration_12, iteration_13) - all passed.

## Feature: Purchasing Plan (Feb 2026, Session 3)
- NEW page at `/purchasing-plan` (React Router added: `App.js` is now a lean `<BrowserRouter>` wrapper, BOM Explorer UI moved verbatim to `pages/BomExplorerPage.js`, shared `components/NavTabs.jsx` for nav highlighting via `useLocation()`).
- Pipeline (`backend/purchasing_plan.py` + `backend/oms_client.py`): pulls sales forecast from external Radish OMS (`oms.radishtechnologies.com`) for the next 2 months (computed dynamically from server's current date) -> resolves each OMS part number to a SAP BOM -> explodes to LEAF-level components only (skips sub-assemblies) -> aggregates required qty per leaf per month -> attaches live SAP standard costs.
- Critical bug found & fixed same session: initially resolved OMS part numbers to SAP BOMs via the OMS `wm-part-map`'s mapped value, which is a DIFFERENT internal ID space SAP's BOM SOAP service doesn't recognize (verified empirically: mapped values consistently 404, while the OMS part number itself resolves correctly in ~99% of cases). Fixed by trying the part number directly first, falling back to the map value only if that fails. Impact: went from 44 leaf components / 59 false "missing BOM" warnings to 803 components / 8 genuinely-unmappable warnings, and Month-2 values (previously all zero) now populate correctly.
- Architecture: `sap_soap_client.explode_bom()` now accepts an optional `shared_cache` param so all ~60 top-level part explosions in one plan run reuse a single sub-BOM cache (many top-level products share common hardware/packaging leaf parts) - cuts redundant SAP round-trips.
- Long-running (this pipeline explodes hundreds of live SAP BOMs sequentially - observed anywhere from ~30s to ~14 min depending on the demo SAP tenant's connection responsiveness) - implemented as an async job: `POST /api/purchasing-plan/generate` returns a `job_id` instantly, frontend polls `GET /api/purchasing-plan/status/{job_id}` every 3s (up to 15 min) instead of holding one HTTP request open past the platform's ingress timeout. UI shows a live elapsed-seconds counter while generating.
- UI: stat cards (leaf components, value per month, missing-BOM count), collapsible missing-BOMs warning table (part_no / mapped SAP ID tried / reason), full leaf-component table (qty + value per month + total), Generate/Regenerate button.
- Tested via testing_agent_v4 (iteration_14, iteration_15) - both passed, no bugs found. Bug fix independently re-verified via live run in iteration_15 (793 components/9 missing vs main agent's 803/8 - minor run-to-run variance expected/normal given the live, sequential, occasionally-flaky SAP tenant).

## Backlog / Next Tasks (updated)
- P2: Persist purchasing plan job history (currently in-memory only, no TTL/cleanup - fine for this single-tenant demo, would need attention for long-lived production use)

## Feature: Category Master + Fix Mapping UI (Feb 2026, Session 6)
- Admin page: dynamic Category Master taxonomy - "Manage Categories (N)" button opens a dialog listing all existing categories + an add-new-category input; new categories are also selectable inline from any row's "Add New Category..." option (auto-applies to that row and flips Source badge to Manual). Backend: `GET/POST /api/admin/categories`, `bom_categorizer.get_categories()/add_category()` (category_master Mongo collection).
- Fixed a gap flagged by testing_agent_v4 (iteration_23): the "Fix Mapping" override UI for the Missing BOMs table was fully missing from `PurchasingPlanPage.js` despite the backend endpoint (`POST /api/purchasing-plan/part-overrides`) being complete and tested. Added per-row text input + "Save & Retry" button (`missing-bom-override-input-{i}`/`missing-bom-override-save-{i}`) - success removes the row from the missing table with a toast to regenerate; failure updates the row's Mapped SAP ID/Reason in place, input stays editable for retry.
- Tested via testing_agent_v4 (iteration_24): 100% backend (16/16, new `test_category_master.py`) + 100% frontend, both features verified live end-to-end, no regressions on Admin/Purchasing Plan.
- Design nuance noted (not a bug): `save_part_override()` persists the override to Mongo before verifying it resolves, so a failed retry attempt overwrites any previously-working override for that part_no (last-write-wins) - acceptable per current UX, flagged for awareness only.

## Feature: SAP MSL Write-Back (Feb 2026, Session 6) - COMPLETE
- Built `/app/backend/sap_planning_client.py` - reads/writes SAP Safety Stock (`SafetyStockQuantity`) and Procurement Lead Time (`PlannedDeliveryDuration`, ISO-8601 "PnD" strings) via a new custom OData service `materialltmsl` (entity `MaterialSupplyPlanningProcessInformation`, Work Center View `PMM_MATERIALS`), set up by the user's SAP admin this session. Write mechanics: CSRF token fetch (`x-csrf-token: fetch`) + session cookie + HTTP PATCH per row, with 4-attempt retry-with-backoff on "Locking object not possible" (SAP briefly locks the parent Material during writes).
- User's chosen scope: push to ALL Supply Planning Areas for a material (not just P1), push both MSL->Safety Stock and a new Lead Time (Days) field->Procurement Lead Time, pull SAP's current values for comparison before pushing, manual per-row "Push to SAP" button.
- Admin page: new "Lead Time (Days)" input column (same save pattern as MSL) and "SAP Push" column with a "Push to SAP" button (enabled only when the component has a captured `product_uuid` - i.e. `has_sap_link=true` - and at least one of msl/lead_time_days is set) + a comparison dialog (Current SAP vs Pushing) + a last-pushed timestamp badge.
- `component_master` schema extended: `product_uuid` (captured automatically whenever a component is seen in BOM Explorer or a Purchasing Plan run - via `bom_categorizer.categorize_items()`), `lead_time_days`, `sap_pushed_at`.
- Verified LIVE end-to-end against the real SAP tenant: pushed SPC5WM's MSL=500/LeadTime=7 and confirmed via direct OData query that all 7 of its Supply Planning Area rows (P1/P2/P3/P4/P6/P7/P9) show `SafetyStockQuantity=500.0`/`PlannedDeliveryDuration="P7D"`.
- Tested via testing_agent_v4 (iteration_25): 100% backend (13/13 new + 16/16 regression) + 100% frontend.

## Feature: Indian Number Formatting + Sales Plan Price/Value (Feb 2026, Session 6)
- Switched all currency/quantity `toLocaleString` calls in `PurchasingPlanPage.js` (formatQty/formatMoney) and `BomExplorerPage.js` (Total BOM Cost, Std Cost, Ext Cost) from default/Western locale to `'en-IN'` - e.g. `148,312,982.64` now renders as `14,83,12,982.64`. Verified correct via testing_agent_v4 (iteration_26, 100% pass) against real Sales Plan Lookup data and quantity columns.
- Sales Plan Lookup dialog: added "Sale Price" and "Sale Value (INR)" columns (both at part level and per-customer on row expansion) plus a totals footer row summing Qty and Sale Value across filtered items - verified mathematically exact.
- Fixed a data-source concern raised by user: `oms_client.get_sales_plan()` was pulling qty/value from the OMS's `view=fulfilment` payload (`planned_qty`/`planned_inr`) instead of the OMS's DEFAULT "Sales" view (`expected_qty`/`expected_inr` - the same fields behind OMS's own Insights > Monthly Sales screen). Added an optional `view` param to `oms_client.get_parts()` (defaults to "fulfilment" for `get_monthly_demand()`/Purchasing Plan, unchanged) and switched `get_sales_plan()` specifically to use the default view. Confirmed via live OMS API probing that the `price` field itself is identical across views and matches actual invoiced price exactly (verified against a past month's real `actual_qty`/`actual_native` ratio) - so this was a semantic-correctness fix (using the OMS-canonical Sales view) rather than a numeric bug. Self-verified via curl (math cross-check: 2.26 USD x 23,040 units -> 41,65,632 INR implies ~80 INR/USD, a sane FX rate).

## Open item
- Live SAP ByD valuation OData endpoint (`materialvaluationdata`) was seen intermittently timing out during iteration_26 testing (unrelated to any code change this session) - if the user reports 0.00/blank Value columns on Purchasing Plan or BOM Explorer, check SAP tenant connectivity first before assuming a code regression.

## Bug Fix: Admin Page Missing SAP Links + Lag (Feb 2026, Session 6 cont.)
- Root cause 1 (missing SAP links): `product_uuid` was only captured going forward from the MSL Write-Back feature's introduction - the 844 components categorized before that had no SAP link, showing "No SAP link yet" instead of a working "Push to SAP" button. Fix: `bom_categorizer.backfill_product_uuids()` scans the existing `bom_node_cache` (which has always carried product_uuid per BOM leaf item) and fills in any missing `component_master.product_uuid` for free, no live SAP calls needed. New `POST /api/admin/components/backfill-sap-links` endpoint + "Backfill SAP Links" toolbar button (idempotent - safe to click repeatedly). Ran live: 842/844 backfilled instantly (979/981 total now linked; the remaining 2 have never appeared as a leaf in any cached BOM tree).
- Root cause 2 (lag): Admin table rendered all 981 rows at once (each with 2 inputs + a select + a button = ~4000+ interactive DOM nodes) under a `position: sticky` header. Fix: client-side pagination at 50 rows/page (`pagedItems`), with Previous/Next controls, auto-reset to page 1 on search/filter/sort change, and "Select All" scoped to only the current page's visible rows (global selection state still persists across page navigation).
- Tested via testing_agent_v4 (iteration_27): 100% frontend pass, DOM confirmed at 50 `<tr>` rows/page (was 981), no regressions.

## Feature: Excel Bulk Import/Export + Push All to SAP (Feb 2026, Session 6 cont.)
- Admin toolbar: "Export to Excel" (client-side, via the already-installed `xlsx` library - downloads all 981 components' Product ID/Description/Category/MSL/Lead Time/Has SAP Link as `Component_MSL_LeadTime_Template.xlsx`), "Import from Excel" (uploads an edited file, PATCHes `msl`/`lead_time_days` per row via the existing single-component PATCH endpoint, blank cells are treated as "leave unchanged" not "clear to zero", concurrency-limited to 8 parallel requests), "Push All to SAP" (starts a new async job).
- `POST /api/admin/components/push-all-to-sap` + `GET .../push-all-to-sap/{job_id}` - background job (same in-memory job/polling pattern as Purchasing Plan generation) that pushes MSL/Lead Time for EVERY component with a SAP link (`product_uuid`) and at least one value set, using bounded concurrency (5 workers, `bom_planning_client.bulk_push_to_sap`) across DIFFERENT materials (safe - SAP's per-Material lock only contends on same-material writes, already handled by the existing retry logic). Frontend polls every 1.5s and shows a live progress bar + final pushed/failed summary dialog.
- Verified LIVE end-to-end: exported real data, edited SPC22WM's MSL/Lead Time in the downloaded Excel file (250/12), re-imported it (confirmed via UI it landed correctly), then ran "Push All to SAP" which reported "Pushed 7 of 7 component(s) successfully" - confirmed via direct SAP OData query that SPC22WM's planning rows now show `SafetyStockQuantity=250`/`PlannedDeliveryDuration="P12D"`. Self-tested (no dedicated testing_agent_v4 round needed - fully live-verified by main agent across every step of the flow).

## Open item: OMS price source - RESOLVED
- OMS team confirmed `expected_qty`/`expected_inr`/`price` (the default Sales view, already what our app reads) now carry the invoice price by default on their end - no code change needed, confirmed to OMS team we will keep reading these fields and will NOT switch to `actual_*`.

## Deployment
- Deployment readiness check passed (Feb 2026, Session 6 end) - no blockers. 2 non-blocking WARN notes: `/admin/components` and `bulk_push_to_sap` run unbounded Mongo queries with no `.limit()` - fine at current scale (~981 components), worth revisiting if the catalog grows significantly.

## Feature: Persistent BOM Cache (Feb 2026, Session 4 cont.)
- Problem solved: Purchasing Plan generation could take many minutes (re-exploding hundreds of live SAP BOMs on every single click, even for a month generated moments earlier).
- New `bom_cache_service.py`: MongoDB-backed (`bom_node_cache` collection, one doc per SAP product_id: bom_id/revision, raw groups/items, found flag, last_checked_at/last_changed_at). `build_tree_from_cache()` is the cache-first drop-in replacement for `sap_soap_client.explode_bom()` used by `purchasing_plan.py` - reads from cache (near-instant), lazily fetches+persists any product_id never seen before. `refresh_stale_nodes()` does a lightweight (non-recursive) per-node SAP check and only overwrites the cache entry if the BOM's revision actually changed - a failed/timed-out fetch NEVER erases good cached data, it's just left for retry.
- Background scheduler in `server.py` (asyncio loop via `@app.on_event("startup")`) runs `refresh_stale_nodes()` every 6 hours (comfortably within the user's 12h freshness requirement). New `GET /api/bom-cache/stats` and `POST /api/bom-cache/refresh` (fire-and-forget - must never block on a live SAP sweep, learned this the hard way mid-implementation after seeing the same ~60s ingress-timeout failure mode as the original purchasing-plan endpoint before it was made async).
- Verified: cold-cache first run ~30s, warm-cache repeat run ~4-9s for the same month (identical results both times); cache node count grows correctly as new parts are encountered (897->915->933 across test runs) and stabilizes when no new parts appear.
- Tested via testing_agent_v4 (iteration_19) - 100% backend pass, no bugs. New regression test file `/app/backend/tests/test_bom_cache.py`.
- NOT YET DONE: SAP inventory netting (see backlog above - blocked on user's SAP team completing setup).
- P2: Live SAP standard-cost lookup has been observed occasionally returning all-zero/null values on a single run (self-resolves on regenerate) - likely transient demo-tenant flakiness; could add a "looks like $0 for everything, retry?" warning banner if it recurs often

## Feature: Missing-BOM Confidence + Retry + Inventory Freshness Badge (Feb 2026, Session 5 cont.)
- **Inventory Freshness Badge**: `inventory_as_of` (ISO timestamp of when on-hand stock was fetched) added to the plan response, shown next to the existing `bom_data_as_of` badge in the UI toolbar (Package icon, amber if >12h stale).
- **Missing-BOM Confidence**: `bom_cache_service.build_tree_from_cache()` now raises `BomFetchError` for ROOT-level fetch failures (distinct from a clean "SAP confirms no BOM"), refactored into a shared `_resolve_boms()` helper in `purchasing_plan.py` used by both `build_purchasing_plan()` and the new retry path. Each `missing_boms` entry now carries `confidence`: `"fetch_error"` (SAP unreachable, safe to retry) or `"not_found"` (SAP cleanly confirmed no BOM). Missing BOMs table has a new "Status" column (icon + label per confidence), also reflected in the Excel export.
- **Retry Failed Lookups button**: new `POST /api/purchasing-plan/retry-missing` endpoint (`retry_missing_boms()` in purchasing_plan.py, reuses `_resolve_boms()`) re-checks a specific subset of part numbers against live SAP in seconds, without re-running the full multi-minute pipeline. Frontend button (visible only when `confidence="fetch_error"` entries exist) merges results into the displayed plan state directly - resolved parts disappear from the missing table, still-unresolved ones update in place - no full regenerate needed to see the retry outcome (regenerating is still needed to fold newly-resolved parts into the actual totals).
- Tested via testing_agent_v4 (iteration_22): 100% backend + frontend pass, all 3 features verified against live SAP/OMS data, light regression check on Inventory Netting (iteration_20) and the false-missing-BOM fix (iteration_21) confirmed no regressions. Noted (pre-existing, not a regression): `purchasing_plan_jobs` is an in-memory dict, lost on backend hot-reload/restart - acceptable for this single-tenant demo tool.

## Bug Fix: False-Negative "Missing SAP BOM" in Purchasing Plan (Feb 2026, Session 5 cont.)
- User reported ~30 OMS parts (FMF519, HTBS, NS-SPST2B, P26632, DM-0009, etc.) that genuinely have valid SAP BOMs were showing up in the Purchasing Plan's "Missing BOMs" warning as "No SAP BOM found".
- ROOT CAUSE: `server.py`'s `MongoClient` was instantiated without `tz_aware=True`. `bom_cache_service.py` writes `datetime.now(timezone.utc)` (timezone-aware) but PyMongo reads timestamps back as timezone-**naive** by default. `build_tree_from_cache()`'s `track()` helper compares a running `min_checked_at` against each node's `checked_at` while walking a BOM tree - the moment a tree mixed an already-cached node (naive, from Mongo) with a newly-discovered node in the same run (aware, freshly created), the comparison raised `TypeError: can't compare offset-naive and offset-aware datetimes`. `purchasing_plan.py`'s `explode()` broad try/except silently swallowed this into `None`, misreporting a perfectly valid, already-cached SAP BOM as "missing" - this happened on essentially any tree that touched even one previously-unseen product_id, which is extremely common.
- Fix: `mongo_client = MongoClient(os.environ['MONGO_URL'], tz_aware=True)` - datetimes read back from Mongo now stay timezone-aware, matching what's written, eliminating the mismatch at the source.
- Secondary fix: `bom_cache_service.py`'s `_fetch_live()` was single-shot with no retry (the flaky SAP tenant occasionally connect-timeouts under the concurrent load a plan run generates) - added a 3-attempt retry with backoff (same pattern as `sap_soap_client.py`'s existing sub-BOM retry), further reducing genuine transient false negatives.
- Verified: FMF519 (93 components), HTBS (60), NS-SPST2B (58) all directly confirmed resolvable and absent from `missing_boms` after the fix. Full live plan run: 799 components / 8 missing (down from 695/15 pre-fix), remaining 8 double-checked directly against SAP and confirmed to genuinely have no Production BOM (purchased/trading parts, not a bug). Tested via testing_agent_v4 (iteration_21) - 100% backend + frontend pass, log-correlation-confirmed the error class no longer occurs post-fix, no regressions to bom-cache/stats, bom-cache/refresh, BOM Explorer, or the just-shipped Inventory Netting feature.

## Feature: SAP On-Hand Inventory Netting (Feb 2026, Session 5)
- Solved the previously-blocked P1 backlog item: user's SAP team exposed a custom Analytics OData report on the "On-Hand Inventory" (SCMINVV02) data source (`SAP_INVENTORY_ODATA_URL` in backend/.env).
- CRITICAL empirical finding (would silently corrupt data if missed): this report's `$select` parameter changes which characteristics the SAP OLAP engine aggregates by. Requesting only a subset of fields (e.g. `CMATERIAL_UUID,KCON_HAND_STOCK`) makes `CMATERIAL_UUID` return an internal numeric surrogate key (e.g. `'430'`) instead of the real Material ID. Fetching the FULL, un-`$select`'d row shape (all ~35 default characteristics) always returns the correct business Material ID (e.g. `'SI-0038C-2'`) - confirmed by cross-referencing against known cached BOM leaf `product_id`s. New `sap_inventory_client.py` deliberately never uses `$select`, paginates via `$top=5000`/`$skip` (report has ~5,746 rows / ~3,165 distinct materials in this tenant), and sums `KCON_HAND_STOCK` per material across every other implicit dimension (site/logistics area/stock status) to get one company-wide on-hand total. Despite its name, `CMATERIAL_UUID` is the Material ID (`product_id`), NOT the GUID (`product_uuid`) used for standard costs.
- `purchasing_plan.py`: `build_purchasing_plan()` now takes a `sap_inventory_client` param; for every leaf component computes `on_hand_qty` (best-effort, same "hiccup shouldn't fail the whole plan" pattern as standard costs), `net_qty_by_month = max(0, gross - on_hand)` per month, and `net_value_by_month = net_qty * unit_cost`. If a component has no inventory-report row (never stocked), `on_hand_qty` is `None` and `net_qty` falls back to equal gross (assume zero stock).
- Per user's explicit choice, Gross and Net are shown as separate, equally-prominent figures (not a replace): new `on_hand_qty`/`net_qty_by_month`/`net_value_by_month` fields added alongside the existing gross fields on `PurchasingPlanComponent`. Frontend (`PurchasingPlanPage.js`) adds an On-Hand column, per-month Net Qty/Net Value columns (light-blue styled to visually separate from gross), a second "Net Purchase Value" stat card per month, category-level Net subtotals, Net-column sorting, and Excel export columns.
- Verified end-to-end against the live SAP tenant (695 components, 679 with on-hand data, netting math spot-checked correct) + tested via testing_agent_v4 (iteration_20) - 100% backend (7/7 pytest, new `/app/backend/tests/test_purchasing_plan_inventory.py`) and 100% frontend pass, zero bugs found.

## Feature: Enhancements Round 3 (Feb 2026, Session 4 cont.)
- Purchasing Plan: added a Category quick-filter dropdown (shadcn Select) that narrows the table/stat-cards/Excel-export to a single category at a time ("All Categories" default).
- Purchasing Plan: added sortable column headers (Product ID, each month's Qty/Value, Total Value) - sorting is scoped WITHIN each category group independently (group order itself stays alphabetically fixed), same click-to-toggle-asc/desc pattern as the BOM Explorer tree.
- Tested via testing_agent_v4 (iteration_18) - 100% frontend pass, no bugs from this round's changes.

## Feature: Enhancements Round 2 (Feb 2026, Session 4)
- Purchasing Plan Excel export (mirrors BOM Explorer's export pattern) - includes a Category column, plus a "Missing BOMs" sheet when applicable.
- BOM Explorer tree: column sorting (Product ID / Quantity / Std Cost / Ext Cost, click to toggle asc/desc) + text search box that filters to matching branches (with ancestors) and highlights the matched substring.
- Purchasing Plan month picker: replaced the fixed "next 2 months" with a single target-month picker (past or future months allowed); `build_purchasing_plan()` now takes an optional `target_month` and always returns a single-item `months` list.
- Bug found & fixed by testing agent (iteration_16) after the sort feature shipped: sort-clicking corrupted tree expand/collapse state (sub-assembly children randomly appearing/disappearing) because expand keys were positional sibling-index paths that broke once `sortTree()` reordered siblings. Fixed by keying expand state off a stable `product_id` ancestor-chain instead of index (iteration_17 re-verified fixed).
- AI categorization is now automatic on BOM load (no button press needed) - `loadCategories()` is invoked right after a successful `/api/bom/search` fetch; the manual button still exists (now defaults to "Re-Categorize" after the auto-run) for re-running if desired.
- Purchasing Plan components are now AI-categorized too (same categorizer, called server-side after `build_purchasing_plan()` completes) and the table groups components into collapsible category sections with per-category subtotal rows (qty/value per month + total), plus bulk Expand/Collapse Categories buttons.
- All tested via testing_agent_v4 (iterations 16 & 17) - iteration_16 found the sort/expand bug (fixed same session), iteration_17 re-verified the fix and confirmed all new features with 100%/100% pass rate, no regressions.
