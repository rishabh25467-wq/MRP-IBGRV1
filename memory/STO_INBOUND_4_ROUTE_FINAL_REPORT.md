# FINAL REPORT: STO Goods Receipt Automation - 4-Route API Investigation (Aug 2026, this session)

Mandate: fully automate STO inbound Goods Receipt via standard SAP APIs only (NO Playwright,
NO ABSL/Cloud Applications Studio, NO manual SAP UI). All 4 routes tested/researched below.

## Route 1: Direct PGRBackground - DEAD (confirmed, SAP-side platform limitation)
- `InboundDeliveryPGRBackground` (custom OData `inboundstockemergent`) returns HTTP 500
  "action is disabled" on every fresh, untouched, Consistent STO Inbound Delivery
  Notification - tested across multiple sites (P1, P3, P8) and even a site (P8) with a
  verified active/Consistent "Standard Receiving, Without Tasks" Logistics Model (`REC_P8`,
  since 2023) - exactly the config SAP's own KBA 2691388 says is required for this action to
  work. Still disabled.
- Prerequisite action `InboundDeliveryRelease` is ALSO disabled with the identical error on
  the same fresh document - the entire release/receipt chain is blocked tenant-wide, not a
  data issue.
- Root SAP KBAs: **3583076** ("Inability to Post Goods Receipt with Actual Quantities via
  OData API in Inbound Delivery Processing") and **2691388** (Action PGR_BACKGROUND not
  possible for STO). Same class of block also hits the outbound side's
  `RequestOutboundDeliveryExecutionRunExecute` ("action is disabled") - strongly suggests one
  tenant-level Business Configuration/licensing gap affects both directions.
- SAP Support ticket already drafted: `/app/memory/SAP_SUPPORT_TICKET_DRAFT_InboundPGR.md`
  (not yet submitted by user as of this session).
- **Verdict: DEAD unless SAP Support/Basis enables the missing config. Not fixable from our side.**

## Route 2: Task-Based Standard Receiving (Site Logistics Task) - DEAD for STO, re-confirmed live TODAY
- Live-queried `QuerySiteLogisticsTaskIn` for site P8 (which has BOTH the "without tasks"
  `REC_P8` model AND the "with tasks" `EM1` model active) TODAY: 128 total Site Logistics
  Tasks. Breakdown: 125 are `OperationTypeCode=21` (outbound Pick tasks), only 3 are
  `OperationTypeCode=11` (Put Away/receiving tasks) - and **all 3 reference PO 29346**, a
  VENDOR PO GRN test document (created via our own direct-SOAP `StandardInboundDeliveryNotification
  BundleCreateRequest_sync` breakthrough), **not any STO**.
- Zero Site Logistics Tasks reference the known real un-received STO (STO-000123, Outbound
  Delivery P1D1-560, ship-to P8, IRON-SCR 5 KGM, GI posted Sep 18 2026, still sitting
  un-received as of this session) or any other STO order ID tried in earlier sessions.
- **Conclusion: EM1's task-based receiving chain is only triggered by OUR OWN SOAP-created
  Inbound Delivery Notification (the vendor-GRN breakthrough flow) - it is NOT triggered by
  SAP's own auto-created STO Inbound Delivery Notification (the one generated automatically
  the moment Goods Issue posts on the shipping side).** These are two different SAP-internal
  document-creation paths with different downstream task-generation behavior, even under the
  identical Logistics Model config. Re-confirms the Sep 18 2026 finding, now double-checked
  against the CURRENT EM1-active state (previous test predated EM1 at some sites).
- **Verdict: DEAD for STO. Task-based receiving only works for our vendor-GRN-created
  notifications, not STO-originated ones.**

## Route 3: API-triggered Inbound Warehouse Request Run (mass data run) - DEAD, no API exists
- Confirmed (SAP docs + community sources): ByDesign has **no standard OData/SOAP API to
  trigger a Warehouse Request/Inbound Delivery mass processing run**. OData in ByD is
  stateless/document-scoped by design; mass runs are UI-scheduled Mass Data Runs (MDRO) with
  no external trigger endpoint.
- The one exception found (during Sep 18 2026 OUTBOUND-side testing) is scheduling an
  "Outbound Delivery Release Run" - itself only startable from SAP UI, not via API - and it
  was inconclusive/didn't pick up fresh test STOs anyway (see `SOAP_GRN_BREAKTHROUGH_2026-09-18.md`).
  No inbound equivalent exists to even test.
- The only way to build genuine "run" automation would be a custom ABSL action inside SAP
  Cloud Applications Studio (SDK), exposed via a new custom OData service - explicitly
  forbidden by the user's mandate (no ABSL/PDI).
- **Verdict: DEAD. No standard API surface exists; only path is forbidden custom ABSL dev.**

## Route 4: 3PL / Externally Managed Receiving - NOT YET TESTABLE, but genuinely NEW & viable
- SAP ByDesign has a dedicated, fully standard **Third-Party Logistics (3PL) / Externally
  Managed Warehouse** scenario built exactly for external systems to post goods receipts
  without ever touching PGRBackground:
  1. `RequestInboundDeliveryExecution` (IDER) - SAP notifies the 3PL system of an approved PO/STO.
  2. `InboundDeliveryReplicationOut` (DDAN) - ASN with delivery/batch info.
  3. **`ProcessInboundDeliveryExecutionConfirmation` (IDEC)** - the 3PL system (i.e. OUR APP)
     sends this SOAP message back to SAP to CONFIRM the goods receipt and CREATE the inbound
     delivery document. This is the standard, supported, non-ABSL mechanism for exactly what
     we need - an external system posting a Goods Receipt into SAP.
- This is a **genuinely different, previously untested avenue** - none of the earlier
  sessions' 9+ documented dead-end attempts (see `STO_CONTEXT.md`) or this session's Routes
  1-3 investigated the 3PL/externally-managed-warehouse path at all.
- Requirements before this can be built/tested:
  1. **SAP Business Configuration**: the receiving site's warehouse must be scoped as an
     "Externally Managed Warehouse" (3PL) in the Warehousing and Logistics work center - a
     real config change, needs the user's SAP functional consultant/Basis team (same class of
     one-time setup as the EM1 Logistics Model work already done for vendor GRN).
  2. **New Communication Arrangement** for the "Third-Party Logistics Provider" communication
     scenario, with our app registered as the 3PL system endpoint (Business Partner master
     data needs a valid DUNS/GLN or `SenderParty` info per SAP KBA guidance, or IDEC calls get
     rejected with "Invalid Internal ID").
  3. Backend dev work (ours, once 1+2 are done): implement the IDEC SOAP message (Inbound
     Delivery ID prefixed `3PLI-`, `TypeCode=1566` for standard supplier inbound - STO's own
     type code needs confirming with SAP docs/support once this is scoped) to post the
     confirmation.
- **Verdict: The only route with genuine, standards-based headroom left. BLOCKED on the
  user's SAP Basis/functional team completing the 3PL warehouse scoping + Communication
  Arrangement first - cannot be tested further from our side until then.**

## Summary table
| Route | Mechanism | Status | Blocker |
|---|---|---|---|
| 1. PGRBackground | Custom OData action | DEAD | SAP-side "action is disabled", KBA 3583076/2691388, needs SAP Support |
| 2. Task-Based Receiving | Site Logistics Task (SOAP) | DEAD for STO | Only triggers for our own SOAP-created vendor GRN notifications, not STO's auto-created ones |
| 3. Warehouse Request Run | Mass Data Run | DEAD | No API trigger exists; only path is forbidden ABSL |
| 4. 3PL/Externally Managed | IDEC B2B SOAP message | UNTESTED/PROMISING | Needs SAP Basis: 3PL warehouse scoping + new Communication Arrangement |

## Recommendation
Submit the already-drafted SAP Support ticket for Route 1 (no cost, might still get unlocked).
In parallel, if the user wants to keep pushing for full automation, Route 4 is the only
standards-based path left - next step is the user's SAP functional consultant scoping ONE
receiving site as an Externally Managed Warehouse + activating the 3PL Communication
Arrangement, after which we can build + test the IDEC confirmation call. Until either happens,
STO receiving stays a manual "Post Goods Receipt As Planned" SAP UI click (current accepted
reality per Sep 18 2026 decision).
