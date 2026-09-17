# BREAKTHROUGH: Direct SOAP GRN Posting (replaces Playwright) - Sep 18 2026

## TL;DR
We can now post a real Goods Receipt into SAP ByDesign via a direct, synchronous SOAP
call - NO browser automation needed. This has been PROVEN LIVE (not simulated) on real
POs. This can eventually replace the fragile Playwright-based GRN posting flow
(`sap_playwright_supplier_pgr_service.py`) entirely, once every site has one piece of SAP
master data configured (see "The one blocker" below).

## The client
`/app/backend/sap_inbound_delivery_notification_client.py` - `SAPInboundDeliveryNotificationClient`.
- `check_maintain_bundle(...)` - pure validation, commits NOTHING. Safe to call anytime.
- `maintain_bundle(..., release=False)` - creates a REAL but unreleased/draft notification.
  Reversible-ish (it's a real doc but not posted/released - can likely be cancelled in SAP).
- `maintain_bundle(..., release=True)` - REAL create+release. This is the one that actually
  posts a Goods Receipt and moves stock. Irreversible like any real GR.

Endpoint: `SAP_SOAP_INBOUND_DELIVERY_NOTIFICATION_ENDPOINT` in `/app/backend/.env`
(`.../managestandardinbounddeliveryn`). Auth: `SAP_SOAP_USERNAME`/`SAP_SOAP_PASSWORD`.

## The ONE blocker that was hiding this working - now understood and partially fixed
SAP rejected every `release=True` attempt with:
> "Logistics model for Standard Receiving with task for site ID {SITE} ({SITE NAME})
> missing. Create a new logistics model for the site with the relevant template in the
> Warehousing and Logistics [Master Data work center]."

This is a real SAP Business Configuration/Master Data gap, confirmed tenant-wide (hit
identically on site P3 AND site P1, not a per-site fluke). It is a NORMAL, self-service
setup step (not a dev/customization task) - see "How to fix per site" below.

### How to fix per site (confirmed working live for site P8, Sep 18 2026)
1. Go to **Warehousing and Logistics Master Data** work center -> **Logistics Models** view.
2. Click **New** -> **New Logistics Model from Template**.
3. Pick Template = **"Template for one-step receiving"** (NOT "Template for receiving
   without tasks" - that's the wrong/opposite variant; we specifically need the WITH-TASK
   one per the error message, and "Automatic Generation of Tasks" checkbox should end up
   checked, "Without Tasks" unchecked).
4. Set **Site** = the site you're fixing (e.g. P8, P1, P3...).
5. Click **Save and Release** directly (do NOT just Save - it sits in "Check Pending"
   status and is NOT usable until released; "Save and Release" runs the check +
   activates it in one step).
6. Once status shows released/consistent (not "Check Pending" anymore), that site's
   `release=True` calls will work.

### Status per site (Sep 18 2026)
- **P8**: Logistics Model "EM1" created + Saved and Released. CONFIRMED WORKING LIVE (see
  test results below).
- **P1, P3**: Confirmed to hit the SAME missing-model error. NOT yet fixed - user needs to
  repeat the same steps above for these sites (and likely every other site: P2, etc.)
  before this works tenant-wide.

## Live test results (Sep 18 2026, real SAP tenant, real POs - NOT simulated)
All ran via a Python one-liner instantiating `SAPInboundDeliveryNotificationClient` directly
with real `.env` credentials (see commands in git/bash history if needed to reproduce).

1. **Check-only** (PO 29703, site P3, vendor R1789/Ray International, IRON-SCR, qty 2 KGM):
   clean success, `has_error: false`, zero notes/severities.
2. **Create without release** (same PO/data): clean success, real UUID + ChangeStateID
   returned = SAP genuinely created a draft record. NOTE: this left a real unreleased
   draft (`DeliveryNotificationID=TESTCREATE-202847`) sitting on PO 29703 in the real SAP
   tenant - user was told to check/cancel it if it's noise.
3. **Full release=True, PO 29703/P3**: REJECTED - "Logistics model...missing" (see above).
4. **Full release=True, PO 28792/P1, vendor H1330/Hamidi Exports**: REJECTED with the exact
   same error, confirming tenant-wide (not P3-specific) gap.
5. **After creating+releasing EM1 for P8**: `release=True` on PO 29346/P8 (vendor H1330,
   product G12LW, 1 EA) -> SUCCESS (first attempt had a harmless date warning "Arrival
   period date must not be in the future" from using a future test date - severity "2"
   warning, not an error). Retried with today's date -> completely clean success, zero
   warnings.
6. **Multi-line test, PO 29346/P8**: single `maintain_bundle()` call with 2 items
   (G12LW/1 EA + GSMW2465-12/1 KGM) in one request -> clean success. Multi-line receipts
   work in a single call, no special handling needed.
7. **Partial-quantity verification**: queried SAP's own live Purchasing Analytics report
   (`SAPPOAnalyticsClient.fetch_open_po_quantities(['29346'])`, already-existing client) -
   PO 29346 now shows item 1 delivered_qty=2/open_qty=98 (out of po_qty=100), item 2 same
   pattern. CONFIRMS: partial quantities post correctly, SAP tracks exactly what was
   delivered and keeps the line genuinely open for future partial deliveries -
   does NOT force/complete the full PO qty. Multiple partial receipts against the same
   line work exactly like the current Playwright flow's behavior.

## Field-name schema - CONFIRMED CORRECT (do not deviate)
During this investigation the user pasted 4-5 DIFFERENT AI-generated XML variants for this
same service, each with different (and WRONG) field names (`DeliveryProcessingTypeCode`/
"IDN", `VendorInternalID`, `SellerParty/InternalID`, `ProcessingTypeCode`=185/188/"185",
`BaseQty`, `TypeCode`, `BusinessTransactionDocumentTypeCode`=23, wrapper suffix "Maintain"
instead of "Create", etc.) - ALL of these were rejected as unreliable/hallucinated because
they contradicted SAP's own official docs (help.sap.com) AND kept changing between
attempts. The schema actually implemented and LIVE-VERIFIED to work is:
- Wrapper: `StandardInboundDeliveryNotificationBundleCreateCheckRequest_sync` (check) /
  `...BundleCreateRequest_sync` (real create/release) - NOT "...Maintain...".
- `<StandardInboundDeliveryNotification actionCode="01" releaseDocumentIndicator="true|false">`
- `<DeliveryNotificationID>`, `<ProcessingTypeCode>SD</ProcessingTypeCode>` (this exact
  field was MISSING before this session - added Sep 18 2026, confirmed required per
  official docs "always SD for standard notifications").
- `<DeliveryDate><StartDateTime>/<EndDateTime>` (full-day window, must not be in the
  future relative to SAP's server date or you get the harmless "Arrival period" warning -
  use TODAY's date, not tomorrow).
- `<VendorID>` (plain, e.g. "H1330", "R1789" - these are `suppliers.sap_internal_id` in
  our own DB, NOT the app's internal `vendor_code` alias necessarily, though they often
  match - confirm via `db.suppliers.find_one({"name": {"$regex": ...}})`).
- Per item: `<Item actionCode="01"><ID>` (sequential line ID for the notification itself,
  not the PO's item number), `<DeliveryQuantity unitCode="EA">`, `ItemProduct/ProductID`
  (elsewhere in the file's item template - see `_ITEM_TEMPLATE`), and
  `ItemBusinessTransactionDocumentReference` -> `PurchaseOrder/ID` (PO number) +
  `PurchaseOrder/ItemID` (PO's real line/item number, e.g. "1", "2").

## Test data used (real, live SAP tenant) - reusable for future tests
- PO 29703 / site P3 / vendor R1789 (Ray International) / product IRON-SCR / KGM
- PO 28792 / site P1 / vendor H1330 (Hamidi Exports) / product F614571 / EA
- PO 29346 / site P8 / vendor H1330 / items: "1"=G12LW/EA, "2"=GSMW2465-12/KGM (both
  po_qty=100, now delivered_qty=2/open_qty=98 after our tests)
- Supplier `sap_internal_id` lookup: `db.suppliers.find_one({"name": {"$regex": "X", "$options": "i"}})`
- Real open PO qty check (no browser, live SAP): `SAPPOAnalyticsClient.fetch_open_po_quantities([...])`
  in `/app/backend/sap_po_analytics_client.py` (already existed, built in an earlier
  session specifically to replace a cancelled Playwright open-qty reader - same spirit as
  this whole investigation).

## NOT yet done / next steps (in priority order per user's last direction)
1. **User to repeat the Logistics Model creation for P1, P3, and any remaining sites**
   (P2, etc.) - same steps as P8's EM1 above. This is the ONLY blocker left before this
   approach works tenant-wide.
2. Once more sites are fixed, build this into the REAL GRN approval flow (likely in
   `supplier_shipment_service.py`, wherever the Playwright PGR call currently happens -
   see `sap_playwright_supplier_pgr_service.py`), with a **feature flag / site allowlist**
   so only sites with a confirmed working Logistics Model use the new direct-SOAP path;
   other sites keep using Playwright until their model is set up too. User explicitly
   asked for this approach (option "b": build now with a flag) vs waiting for all sites -
   awaiting final confirmation on which to do first.
3. Clean up the stray uncommitted-but-real test records left in the live SAP tenant if
   they bother anything downstream (TESTCREATE-202847 on PO 29703, TESTPOSTP8B-204607 +
   TESTMULTI-204655 on PO 29346, TESTPOSTP8-204540 also on PO 29346 - these are real
   receipts now sitting on real POs' delivered quantities, not blocking but worth knowing
   about for reconciliation).

## Separate, still-open investigation from same session: Goods Issue automation
A DIFFERENT custom endpoint was also surfaced during this session, NOT yet tested:
`https://my431827.businessbydesign.cloud.sap/sap/bc/srt/scs/sap/ygoodsissuemaintainin`
(a "Y"-namespace custom service, likely tied to the "Goods Issue Maintain in" row seen in
a Communication Scenario admin screen). Tried fetching its WSDL via plain GET / `?wsdl` /
etc - all return `415 Unsupported Media Type` with empty body, so its schema could NOT be
introspected automatically. STILL NEED: user to pull the real WSDL from SAP's Service
Explorer (Application and User Management -> Input and Output Management -> Service
Explorer, search "GoodsIssueMaintain" or similar) before attempting anything here - do
NOT guess field names for this one either, same lesson as above. This is a SEPARATE,
unrelated avenue from the Inbound Delivery Notification work above - would be for
automating STO Goods Issue (currently manual per the Sep 18 architecture decision), not
GRN receiving.

## Does this Logistics Model issue affect STO creation/receiving too? (investigated, answered)
User asked whether creating a multi-line STO is also restricted by this same "Logistics
model" gap. Investigated thoroughly by querying the real `stock_transfer_orders` DB
collection (99 real STOs) - answer is **NO, this is NOT an STO problem**:

- **STO creation (outbound, ship-from site)**: searched all 99 STOs' `error_message` field
  for "logistics model" - zero matches. Historical creation failures are for unrelated
  reasons (sourcing master data, `GET_AVAIL_CONF` disabled, missing planning data, etc.).
  Also confirmed **51 multi-line STOs exist**, **50 of them successfully reached SAP**
  (`status: created_in_sap`, real `sap_order_id` assigned) across many site pairs (P1<->P8,
  P9->P2, etc.) - multi-line STO creation demonstrably already works fine today.
- **STO receiving (inbound, ship-to site, via Playwright)**: searched all STOs'
  `receipt_error` field for "logistics model" - zero matches, including on site P1 (which
  DID hit the "Logistics model...with task...missing" error in this session's direct-SOAP
  GRN tests). Historical P1 receipts show many successful (empty-error) receipts via the
  existing Playwright flow.
- **Conclusion**: the "Standard Receiving (with task)" Logistics Model gap is SPECIFIC to
  the new experimental direct-SOAP `StandardInboundDeliveryNotificationBundleCreateRequest_sync`
  approach used for supplier PO GRNs (this apparently requires/triggers formal warehouse
  TASK auto-generation). The EXISTING STO creation path and the EXISTING Playwright-driven
  STO receiving path use different SAP mechanisms that do NOT hit this same requirement -
  they were unaffected before this session and remain unaffected now. No STO-side action
  needed because of this finding.

`https://my431827.businessbydesign.cloud.sap/sap/bc/srt/scs/sap/ygoodsissuemaintainin`
(a "Y"-namespace custom service, likely tied to the "Goods Issue Maintain in" row seen in
a Communication Scenario admin screen). Tried fetching its WSDL via plain GET / `?wsdl` /
etc - all return `415 Unsupported Media Type` with empty body, so its schema could NOT be
introspected automatically. STILL NEED: user to pull the real WSDL from SAP's Service
Explorer (Application and User Management -> Input and Output Management -> Service
Explorer, search "GoodsIssueMaintain" or similar) before attempting anything here - do
NOT guess field names for this one either, same lesson as above. This is a SEPARATE,
unrelated avenue from the Inbound Delivery Notification work above - would be for
automating STO Goods Issue (currently manual per the Sep 18 architecture decision), not
GRN receiving.
