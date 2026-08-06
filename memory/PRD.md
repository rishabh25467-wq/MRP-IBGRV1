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

## Backlog / Next Tasks
- P1: Excel/CSV export of search results
- P1: Bulk/all-BOMs pull mode
- P2: Persist search history in MongoDB
- P2: Material Master lookup to resolve MaterialUUID → readable Material ID when InternalID is absent
- P2: Multi-level (nested L3/L4/L5) BOM hierarchy support
