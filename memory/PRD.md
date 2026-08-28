# PRD - SAP BOM Viewer / Production Planning App

> Detailed, dated implementation history lives in `/app/memory/CHANGELOG.md` (append new entries at
> the top). This file is the static problem statement + architecture + current backlog only.

## Original problem statement
Extend a SAP BOM viewer application into a full production-planning suite for Radish Technologies:
- Production Plan page integrating an external OMS Open-PO Demand feed; 2-Tier MRP system.
- Microsoft Entra ID (Azure AD) SSO for internal staff.
- Supplier Portal (external vendors) with its own JWT auth (Emergent Auth).
- Purchase Order Creation automation.
- App-side Inbound STO Receipt page that groups multiple SAP delivery lines into one clickable row.
- Headless Playwright automation to perform SAP Goods Receipts (PGR) where native SAP APIs block it.

## Architecture
- `/app/backend/`: FastAPI + MongoDB. SAP ByDesign integration via SOAP and OData clients
  (`sap_sto_client.py`, `sap_gsa_write_client.py`, `sap_inbound_delivery_client.py`,
  `sap_goods_movement_client.py`, `sap_outbound_delivery_client.py`,
  `sap_playwright_pgr_service.py`), plus Microsoft Entra ID SSO + custom JWT (Supplier Portal) auth.
- `/app/frontend/src/`: React SPA, Shadcn UI components.
- Key 3rd-party integrations: SAP Business ByDesign (SOAP + OData +, as of this session, real UI
  automation via Playwright for Goods Receipt), Microsoft Entra ID SSO, Emergent Auth (JWT),
  OpenAI GPT-5.4-mini (Emergent LLM Key) for AI Quick Entry, Emergent Object Storage.

## Core features (built)
- BOM Explorer, Production Plan / 2-Tier MRP, Purchasing Plan.
- Stock Transfer Orders (STO): create, release, Goods Issue (per-line and combined-delivery attempts),
  Inbound STO Receipt (groups SAP delivery lines, posts Goods Receipt via Playwright - see CHANGELOG).
- Supplier Portal: PO acceptance, shipment creation, GRN admin approval + SAP GSA posting.
- Purchase Order Creation automation.
- Internal GRN admin approval + posting (Goods Movement).

## Known SAP-side structural limitations (do not re-investigate, already conclusively proven)
- `PGRBackground` (Post Goods Receipt) via OData is disabled for this tenant's Inbound Delivery
  Notifications - official SAP KBA 3583076 confirms Actual Quantity can't be passed this way. Fixed
  via Playwright UI automation (`sap_playwright_pgr_service.py`).
- Site Logistics Task API doesn't apply - this tenant has 0 task-based inbound execution tasks.
- GSA (Goods and Service Acknowledgement) only works for non-stock/service PO lines, never real stock
  materials.
- No Inbound Delivery Notification is created at all for PO-sourced (external supplier) stock items in
  this tenant - only Stock-Transfer-sourced ones exist. Root cause of why Supplier Portal GRN used GSA
  as a (broken, for stock items) workaround.
- A multi-line STO producing ONE combined Outbound Delivery (instead of one per line) is NOT possible
  via any SAP API - 8 documented dead-end attempts (PGIInBackground, SLRequestDeliveryExecution,
  OutboundDeliveryRequestAllocate, SLPGIInBackground, PartialDeliveryControlCode="3") - this tenant's
  Ship-to Party Account Master Data silently forces per-line deliveries regardless of payload. Fixed
  via a second Playwright UI automation (`sap_playwright_outbound_gi_service.py`, Aug 28 2026) - see
  CHANGELOG. Vehicle No./Transportation Mode/Place Of Supply/G.R No./Date Of Supply/G.R Date are real
  SAP extension fields (`_KUT` suffix) but reject direct API writes once SAP's scheduler picks up the
  document - only writable via the SAP UI, which this same Playwright script now also does for
  multi-line orders. "Freight Forwarder" needs a real SAP Business Partner lookup (breaks Consistency
  Status as free text) - stays Note-only, deliberately not attempted via UI.

## Current backlog

### P0
- Wire Supplier Portal GRN (external, stock items) to a Playwright automation - investigation IN
  PROGRESS (2026-08-28 session, see CHANGELOG): confirmed no IDN or GSA path exists for real stock PO
  items, BUT found a promising new lead - "Inbound Logistics -> Purchase Orders" work center has a
  real, enabled "Post Goods Receipt" button directly at the PO level for released POs (no IDN needed).
  User explicitly paused further investigation ("I'll tell u exact windows to follow later") - resume
  by picking a genuinely "Ordered"/"In Process" PO with real un-received stock and read-only inspecting
  that Post Goods Receipt screen's fields, per user's next instructions. Do NOT click Post Goods
  Receipt on a real PO without the user's exact steps.
- Possible data-integrity gap flagged (not yet investigated further, user said to leave it for now):
  Supplier Portal PO cache may be showing external vendors POs that are still "In Preparation"
  (unreleased draft) in SAP as if they were open orders to fulfil - PO 28792 was found in this state.
- Phase 5: External QMS feed - secured API/data feed (API key) for external QMS app to pull
  pending-QC items.

### P1
- Multiple SAP business users for Playwright concurrency (2-3, possibly split by warehouse) - would
  fix a real risk where concurrent jobs sharing one SAP login can force-kick each other via "Delete
  all sessions?" on login. User said "I'll decide, revisit later" (2026-08-28) - needs 2-3 new SAP
  UI-capable business user credentials from the user before implementing.
- e-Way Bill and e-Invoice integration via Sandbox.co.in - blocked on user providing API credentials.
- Per-Line Ship Status - STO detail screen should show each line's own shipped/pending status, not one
  combined status.
- Auto-Refresh After Transfer - refresh a site's stock right after its own STO posts.
- Import Preview - before/after diff table before committing a bulk Excel import.
- Missing Weight Alert - flag components with no weight data in the source report.
- Auto-Retry ERP Sync - automatically retry a failed ERP sync a few times in the background.
- Goods Movement "RESTRICTED Quality Inspection" stock status - rejected by SAP tenant currently;
  GRN step 2 does a plain move only (see test_credentials.md for the live-tested finding).
- Non-blocking background "Receive" dialog + multi-select bulk STO Inbound Receipt posting (frontend
  UX only - lets user close the Inbound Receipt modal while the Playwright PGR job runs, and select
  several STOs to receive at once). Independent of the Outbound combine fix (Aug 28 2026).
- Observability gaps flagged by testing agent (iteration 132, not bugs, no regression found): (1)
  single-line STOs still don't get the 5 SAP `_KUT` metadata fields (only multi-line/Playwright path
  writes them) - decide if single-line should also route through Playwright just for metadata; (2) a
  silently-failed metadata fill inside `_fill_delivery_metadata` doesn't get flagged on the STO doc.

### P2
- Fix stuck historical SAP orders (30280/30336/30385) - blocked on user + SAP consultant.
- Delivery Note Batch Print, Fuzzy Product Match (AI Quick Entry), Bulk Finish (reporting points),
  Category Picker (Create Material dropdown from SAP's Product Category list).

### P3 (future)
- Backfill old CompCode on already-synced legacy ERP rows.
- Incorporate Quality/OTIF into AI Quota Suggestions.
