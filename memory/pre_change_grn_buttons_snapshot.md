# Snapshot before Sep 20 2026 "remove Create SAP Notification button + strip warehouse movement" change

Taken BEFORE any code changes for this task, purely for revert reference if the user ever asks
to go back. (Reminder: for an actual code revert, direct the user to the platform's "Rollback"
feature on a checkpoint before this change - this file is just a human-readable description of
what existed, to help decide WHICH checkpoint / what to restore if rollback isn't precise enough.)

## What existed before this change (`/app/frontend/src/pages/GrnApprovalPage.jsx`)

Two action buttons shown side by side on every actionable (pending) shipment, for EVERY site:

1. **"Create SAP Notification"** (green, `bg-[#027A48]`, `data-testid="grn-approve-button"`)
   - `onClick={() => openApprovalConfirm("normal")}`
   - Available at ALL sites (not gated by site).
   - Per the "Manual GRN mode" toggle description: "Approvals only create the SAP Notification -
     you post the Goods Receipt yourself in SAP" - i.e. this button creates the Inbound Delivery
     Notification only; a human then goes into SAP UI and posts the actual Goods Receipt manually.
   - This was the ONLY functional action button for any non-P8 site's manual GRN workflow.

2. **"Test Full Automated GRN"** (purple, `bg-[#6941C6]`, `data-testid="grn-approve-full-auto-button"`)
   - `onClick={() => openApprovalConfirm("full_auto")}`
   - **Disabled unless `siteId === "P8"`** (tooltip: "Test Full Automated GRN is only set up for
     site P8 right now").
   - Full EM1 automation: Notification -> release -> Goods Receipt post -> Put Away Task confirm
     -> Goods Movement (stock relocation into the shipment's chosen warehouse). This is the flow
     covered by ALL of today's earlier session work (idempotency fix, per-item lock, retry-on-lag,
     escalation flagging - see PRD.md Sep 20 2026 entries).

Plus a **"Warehouse" dropdown** (`data-testid="grn-warehouse-select"`) next to the Site dropdown,
used to pick the shipment's target warehouse for the (now-being-removed) Goods Movement step.

## Backend endpoints involved (unchanged by this task, for reference)
- `POST /api/admin/grn/{doc_code}/approve` - "normal"/Create SAP Notification path.
- `POST /api/admin/grn/{doc_code}/approve-full-auto` - "full_auto"/Test Full Automated GRN path,
  triggers `_start_full_auto_grn_job` -> `_auto_finish_full_auto_grn` (server.py) which does the
  Put Away confirm + Goods Movement automation being removed today.

## This task's change (Sep 20 2026, explicit user request)
- Remove "Create SAP Notification" button entirely, for ALL sites.
- Rename "Test Full Automated GRN" -> "Post GRN in SAP", made available at all sites (was P8-only).
- Strip the Put Away confirmation + Goods Movement automation out of that flow - it now only does
  Notification -> release -> Goods Receipt post, then stops (no warehouse relocation attempted).
- Remove the "Warehouse" dropdown from the UI (no longer needed once movement is stripped out).
- Reason given by user: wanted to eliminate any automatic warehouse movement risk while the SAP-side
  goods-movement issues from earlier today (idempotency bug, race condition, SAP query ABAP dump -
  see PRD.md) are being sorted out; asked for GRN posting only, no downstream automation, for now.
