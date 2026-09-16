# Landed Cost Feature - Design Discussion (Sep 16 2026 session)
Status: DISCUSSION ONLY, NOTHING BUILT YET. User explicitly said "DO NOT BUILD anything, all discussion"
mid-way through this thread - every decision below is LOCKED DESIGN, pending an explicit go-ahead to build.

## 1. Problem
Handle freight/customs/insurance/loading costs on inbound shipments (ex-works and third-party
transporter invoices) so they get correctly allocated across the received goods and reflected in
SAP's own inventory valuation (Moving Average Price) - not just an internal side-calculation.

## 2. Allocation basis waterfall (LOCKED - 2 tiers, no 3rd tier)
- **Tier 1 - Weight**: applies ONLY when EVERY line item in the landed-cost invoice's scope is
  ordered in a KG-based unit of measure (raw metal stock bought by weight - steel, coils, brass,
  copper). The "weight" is simply that line's own PO/Invoice quantity (already in KG) - NO separate
  product-weight-master lookup needed (confirmed: PO/GRN weight data is "not always reliable", so
  deliberately NOT using `component_master.net_weight_kg`/`gross_weight_kg`, even though that field
  already exists in this app for the BOM scrap-calc feature - user's explicit call).
- **Tier 2 - Invoice Value**: used for everything else. Allocation is PROPORTIONAL to each line's own
  value (line_value / total_invoice_value), never an equal per-line split. Example confirmed with user:
  invoice with lines worth 1,00,000 / 30,000 / 10,000 (total 1,40,000) -> a 7,000 freight charge splits
  71.4% / 21.4% / 7.1% = 5,000 / 1,500 / 500.
- **Tier 3 (PO Line Value) - REMOVED**, not needed: user confirmed (a) the GOODS invoice is a mandatory
  precondition before any supplementary/landed-cost invoice can even be posted (so invoice value is
  ALWAYS available by the time we allocate), and (b) if the goods invoice is under dispute/on-hold, the
  supplementary invoice + its allocation are simply BLOCKED/DEFERRED entirely until resolved - never
  partially processed against an unreliable value. No scenario left needing a PO-value fallback.
- **Rule**: always ONE method for the WHOLE invoice, never mixed per-line within one document (e.g. if
  an invoice has some KG-ordered lines and some non-KG lines, the WHOLE invoice drops to Tier 2 - never
  "3 lines by weight, 1 line by value" in the same document).

## 3. Cost types covered
Freight, Customs Duty, Insurance, Loading/Unloading - each is its own "landed cost component" in SAP,
each needs its own GL account mapping (see section 6).

## 4. Multi-status GRN handling (LOCKED)
- One landed-cost invoice can span MULTIPLE partial GRNs (different receipt dates/qty) of the same
  shipment - cost must be split across all of them (waterfall computed once, applied across every GRN
  line in scope).
- A single GRN can itself have mixed line statuses (some accepted, some rejected/QI) - cost only
  allocates to the ACCEPTED qty. The REJECTED qty's proportional share is NOT sent to SAP's allocation
  document at all (0% - so it never pollutes that stock's Moving Average Price) - instead it's recorded
  as an INTERNAL-ONLY "supplier debit" record (product, PO/GRN ref, amount, reason) for AP/finance to
  raise an actual debit note against the supplier. Never pushed to SAP.

## 5. Where it lives in the app (LOCKED)
- Standalone "Landed Cost" page (NOT squeezed into the GRN detail page) - because one invoice can span
  multiple GRNs across different receipt dates, and the invoice itself usually arrives well AFTER the
  GRN (transporters bill weekly/monthly). A single-GRN-scoped screen couldn't naturally pull in sibling
  GRNs from the same shipment.
- GRN page gets ONE lightweight addition: an optional "Bilty/LR No." field, captured at receipt time
  (it's genuinely known then, on the physical paperwork) - purely informational, no cost logic. This
  becomes the natural search/match key later when the Landed Cost page needs to pull in the right GRNs.

## 6. SAP write-back mechanism (LOCKED - real native SAP feature, not a custom shadow calculation)
Researched via web search (help.sap.com + SAP Community blogs) - SAP ByDesign has a REAL native
"Landed Cost" process:
1. Post a **Supplier Invoice with NO PO reference** (`ManageSupplierInvoiceIn`) - each cost type
   (Freight/Customs/Insurance/Loading) is its own invoice item, each tagged with its own "Landed Cost
   Component" (this tag is what determines the correct dedicated GL clearing account - we never send a
   GL account number directly; it's derived from SAP's own Account Determination config).
2. Create + release an **Allocation Document** (`ManageAllocationDocumentIn`, operations
   `CheckMaintainBundle` / `MaintainBundle`) linking that invoice's item(s) to the relevant
   `InboundDeliveryID`/`InboundDeliveryItemID`(s) from the GRN(s), each carrying OUR computed `Percent`
   (SAP's allocation doc also natively supports auto-distribute by weight/net-value/quantity - but we
   feed our own app-computed % directly instead, since SAP's native "weight" distribution likely relies
   on the same unreliable product-weight-master data we already decided NOT to use).
3. On release, SAP AUTOMATICALLY recalculates the Moving Average Price of the received stock - this is
   the only way to get true landed-cost-adjusted inventory valuation; a side dashboard couldn't do this.
- **One-time SAP Business Configuration prerequisite** (SAP admin/consultant, NOT doable via our API):
  define the 4 Landed Cost Components (Freight/Customs/Insurance/Loading) in Purchase Requests and
  Orders WC, each assigned a Landed Cost Category (capitalized vs expensed); set up GL Account
  Determination for each category (3 dedicated account types: Unbilled Payables, In Transit, Expenses -
  keeps landed costs off the normal GR/IR account).
- **Service exposure - CONFIRMED LIVE ALREADY ACTIVE (Sep 16 2026, this session)**: verified via SAP's
  own WSIL service listing (`GET https://my431827.businessbydesign.cloud.sap/sap/bc/srt/wsil?saml2=disabled`,
  authenticated as our existing `_EMERGENTBOM` technical user - a WSIL only lists services actually
  reachable by the authenticated user, so this is a real confirmation, not a guess):
  - `ManageAllocationDocumentIn` -> WSDL: `https://my431827.businessbydesign.cloud.sap/sap/bc/srt/wsdl/sdef_/SRMAP/MANAGEALLOCATIONDOCUMEN/wsdl11/ws_policy/document?sap-vhost=my431827.businessbydesign.cloud.sap`
    (fetched successfully, 24.7KB, real operations confirmed: `CheckMaintainBundle`, `MaintainBundle`)
  - `ManageSupplierInvoiceIn` -> WSDL: `https://my431827.businessbydesign.cloud.sap/sap/bc/srt/wsdl/sdef_MANAGESUPPLIERINVOICEIN/wsdl11/ws_policy/document?sap-vhost=my431827.businessbydesign.cloud.sap`
    (fetched successfully, 360.7KB)
  - Both WSDLs saved locally at `/tmp/allocation.wsdl` and `/tmp/supplierinvoice.wsdl` in this session's
    container (NOT guaranteed to persist across forks/restarts - re-fetch via the same WSIL+WSDL URLs
    above if gone; same `_EMERGENTBOM` credentials already in `backend/.env`).
  - **No SAP admin request needed for service activation** - this blocker is CLEARED. (GL/cost-component
    business config from the bullet above is still a separate, real prerequisite, still not done/checked.)
  - Note on how this was confirmed: a naive `GET ...?wsdl` on these (and even on a KNOWN-WORKING existing
    endpoint like the STO service) returns HTTP 415 - this is NOT meaningful, ByDesign SOAP runtime
    endpoints don't serve WSDL via that convention. The WSIL (`/sap/bc/srt/wsil?saml2=disabled`) + its
    per-service WSDL links is the correct, reliable way to check "is this service really active for our
    user" - use this method for any future new-service check in this app instead of a bare `?wsdl` GET.

## 7. Enforcing invoice-post + allocation are never left half-done (LOCKED)
Real risk: `ManageSupplierInvoiceIn` and `ManageAllocationDocumentIn` are 2 SEPARATE API calls, not one
atomic SAP transaction - invoice can post successfully then allocation can fail (timeout/validation
error), leaving a real posted invoice with NO allocation and inventory valuation never corrected.
Mitigations (all locked):
1. Sequential, same-flow attempt (not decoupled fire-and-forget jobs) - same pattern as this app's
   existing STO submission (Check -> Maintain back-to-back).
2. Once invoice posts, its real SAP `InvoiceID`/`InvoiceItemId`s are persisted immediately - retry NEVER
   re-posts the invoice, only re-attempts the allocation call using the saved IDs.
3. Record status is never "Completed" until the Allocation Document is actually released by SAP -
   "invoice posted, allocation pending" is a distinct, clearly-flagged intermediate state.
4. A record stuck in that intermediate state surfaces in the existing "Action Needed" admin
   notifications panel (same mechanism as other stuck-SAP-state cases already in this app) with a
   one-click "Retry Allocation" button.
5. Pre-flight validation (every referenced GRN/inbound-delivery-item is real+fetchable, goods invoice
   posted+undisputed) happens BEFORE either SAP call fires, to reduce how often step 2 is even reached.

## 8. Final UX flow (LOCKED - 2 buttons, not 3)
Considered 3-button (Preview -> Post Invoice -> Allocate) vs 2-button. Decided 2-button is equally safe
because the only genuinely risky/hard-to-undo step is ALLOCATION (changes inventory valuation) -
POSTING the invoice alone doesn't touch valuation and is still correctable in SAP if wrong. So:
- **Button 1: "Preview & Post Invoice"** - user enters invoice header + cost lines + picks GRNs/Bilty;
  app computes the % split (shown on screen) AND posts the invoice to SAP in the same click
  (`ManageSupplierInvoiceIn`), saving real `InvoiceID`/item IDs. Status -> `invoice_posted`.
- **Button 2: "Allocate & Release"** - enabled ONLY when status = `invoice_posted` (enforced BOTH in UI
  AND backend - backend must reject an out-of-order/duplicate call even if hit directly via API, not
  just grey out the button). Calls `ManageAllocationDocumentIn` with the saved invoice IDs + the same
  computed %. Success -> `completed` (SAP has updated MAP). Failure -> stays `invoice_posted`, retry-able
  without ever re-posting the invoice.

## 9. Open items / not yet decided
- Exact GL account per cost-type component - needs confirming with SAP admin (business config, not
  something this app's code decides).
- Whether the goods-invoice-must-be-posted-first check should look at `sap_supplier_invoice_client.py`'s
  existing read capability (currently READ-ONLY, no write/posting capability exists yet - confirmed by
  grep, nothing in this codebase calls `ManageSupplierInvoiceIn` or `ManageAllocationDocumentIn` today).
- No `sap_allocation_document_client.py` file exists yet - will need to be created from scratch using the
  real WSDL fetched in section 6 (not generic SAP help-doc XML samples) once building starts.
- User has not yet said "start building" - last question asked (Sep 16 2026) was whether to (1) start
  building now, (2) keep discussing, or (3) pause - AWAITING ANSWER (this doc-request came before that
  answer; check latest user message for the actual decision before writing any code for this feature).

---

# Related bug work this same session (separate from Landed Cost, but same thread)

## A. STO-000415 print preview missing price - FIXED, deployed to preview only (not yet to production)
See PRD.md "Session (Sep 16 2026)" entry for full detail. Summary: `sync_to_erp_portal()` froze
`rate`/`amount` on an STO's items at ERP-sync time; if that one-time SAP valuation lookup came back
empty, the frozen value stayed None/0 forever. Fixed by making `get_delivery_note_data()` (both
`/delivery-note` and `/delivery-note/excel`) do a live SAP price retry + self-heal for any item with a
missing/zero rate. Live-verified on 4 real preview-DB orders (STO-000032/039/042/050). Deployment
readiness scan passed (deployment_agent) - user still needs to click "Deploy" in the Emergent UI for
this to reach production; STO-000415 itself only exists in production's DB (not this preview's), so it
could not be directly re-tested here - it should self-heal the next time its print preview/export opens
post-deploy, PROVIDED the actual root cause there is the same frozen-zero-price pattern (matches the
5+ real cases reproduced locally) and not something else. NOT YET CONFIRMED FIXED BY USER on production.

## B. BOM Explorer parent/child price mismatch - investigated, NOT a bug (2 separate findings)
1. Parent assembly showing a cost that doesn't equal the sum of its visible children is EXPECTED, not a
   bug: `getEffectiveCost()` in `BomExplorerPage.js` only rolls up from children when the parent has NO
   direct SAP cost (or an explicit 0). If the parent DOES have its own direct SAP Standard
   Cost/Moving-Average price (as "100162725F" -> INR 115.64 did), that's shown as-is and children are
   never even looked at - the direct cost is a SAP-side snapshot (labor+overhead+material from a past
   Cost Estimate run) that does NOT live-recalculate whenever a child's own price changes later.
2. Separately, product TYT18755 ("YQ 8/18-55-187-450N GAS SPRING") showing an implausible INR 2.74 (vs
   its own consistent ~INR 90-140 history across every other site) is a REAL SAP DATA ANOMALY, not an
   app bug - confirmed via a direct live OData query against `MaterialValuationDataValuationPriceCollection`
   for this product's UUID (`fbef93f3-ed98-1edf-99e9-ecfe58ddb675`): at valuation level
   `EF759B0A-2935-1EEF-B0BA-0FDBDD27E650` (Site P2, `PermanentEstablishmentUUID`
   `635BA7F2-7D13-1EDD-B8D1-AD5E5DDCA263`), a genuine SAP price row has `Amount: 2.740000`,
   `PriceTypeCode: 1` (Moving Average), `StartDate` in effect as of now, open-ended `EndDate` - i.e. SAP
   itself currently holds this as the live Moving Average price for that site, almost certainly a data
   entry error (likely meant ~274.00 or a correct ~108-114 continuing the historical trend). This app's
   code is faithfully reflecting real (bad) SAP data, not miscalculating anything.
   BOM Explorer's `get_standard_costs` (no `site_id` passed for this caller) picks "whichever
   currently-valid row has the latest StartDate across ALL sites" - since P2's bad Aug/Sep 2026 entry has
   the latest StartDate of any site, it wins and is shown for the whole BOM view, masking the other
   sites' correct ~108-140 values. Recommended user action (not yet done): ask SAP admin/inventory team
   to check recent Goods Receipts/price updates for TYT18755 at Site P2 around that date for a bad
   per-unit price entry, and correct it directly in SAP - once fixed there, this app will reflect it
   automatically, no code change needed here.
