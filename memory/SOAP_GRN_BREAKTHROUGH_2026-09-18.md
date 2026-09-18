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

## CRITICAL STATUS CHANGE - Sep 18 2026: Playwright automation STOPPED
User clarified (this was a STANDING instruction from an earlier session, not new): all
Playwright automation is stopped and must NOT be used for any real actions - both
`sap_playwright_supplier_pgr_service.py` (vendor PO GRN posting) AND
`sap_playwright_pgr_service.py` (STO inbound receiving) are affected. Staff currently do
BOTH vendor GRN posting and STO receiving MANUALLY, directly in SAP's own UI, as the
current stopgap - NOT fully blocked, just non-automated right now. No code-level
kill-switch exists for this (grepped `.env`/`server.py`/`*.py`, none found) - this is an
operational/policy stop, not a code flag. Do NOT call into either Playwright service for
any real action going forward. Building the direct-SOAP replacement is confirmed as the
next top priority, starting with vendor GRN (already proven working, see above).

IMPORTANT DISTINCTION - the proven SOAP breakthrough above (StandardInboundDeliveryNotification
BundleCreateRequest_sync) is specifically for VENDOR PO GRNs, where WE create a brand new
Inbound Delivery Notification from scratch against a Purchase Order (no SAP delivery
document exists yet). This is technically DIFFERENT from STO receiving: for an STO, SAP
already auto-creates the Inbound Delivery document itself the moment Goods Issue is
posted on the shipping side - receiving an STO means POSTING GOODS RECEIPT against that
EXISTING delivery, not creating a new notification. This likely needs a DIFFERENT SAP
service/action (something in the "ManageInboundDeliveryIn" family, or similar - NOT yet
researched). Do not assume the vendor-GRN breakthrough automatically also solves STO
receiving - that needs its own separate investigation before building anything for it.

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

## LIVE TEST IN PROGRESS: EM2 (Standard Shipping with tasks) - Site Logistics Task observation
User created **EM2** for site P8: Type=Standard Shipping, Template="one-step shipping with
pick lists" (the WITH-TASK shipping variant, mirrors EM1's receiving fix), Release Outbound
Delivery = **Manually** (intentionally NOT auto-release yet, per the cautious step-by-step
plan). Saved and Released successfully (status: Consistent). Note: "Automatic Generation
of Tasks" was UNCHECKED by this template's default (unlike EM1's receiving template which
had it checked) - this template may expect a manual "Create Pick List" step in SAP UI to
actually generate the task, not automatic.

Created a REAL test STO to observe: **STO-000109** (P8 -> P1, product 09200726-01,
qty 1 EA, source_warehouse_id P8-RM) via the app's real `/api/stock-transfer/orders`
endpoint (using the `grnscreenshot...` super_admin test session cookie - see
test_credentials.md). Result: created fine in SAP (`sap_order_id`
00000000000000000000000000000032291, `gi_delivery_request_id: "61095"`), `gi_status:
"awaiting_manual_gi"`, `outbound_delivery_ids: []` (Outbound Delivery not yet created -
that only happens once GI is actually processed, which is manual per the Sep 18
architecture decision - so this STO is currently just sitting at the Delivery
Proposal/Request stage, not yet a real Outbound Delivery).

BLOCKED on observing whether a Site Logistics Task now exists for this STO: the
`SAPSiteLogisticsQueryClient.find_tasks_for_site()` (already-existing, from "attempt #8")
is currently returning a generic SOAP fault ("An exception was raised", SY530 - the same
uninformative generic fault as elsewhere in this session) for BOTH P8 (new) AND P2 (a
site we did NOT touch, which per this same file's earlier note used to return 18 real
tasks successfully in an "attempt #8" session). This strongly suggests either a transient
SAP-side issue right now, OR something changed on SAP's side affecting this query service
tenant-wide - NOT something caused by our EM2 change specifically (since an untouched
site P2 fails identically). AWAITING user to either (a) let it be retried later, or
(b) check directly in SAP's own UI (Warehousing and Logistics -> Site Logistics Tasks,
site P8) whether a task exists for STO-000109 / delivery request 61095.

## Hypothesis raised by user: could EM2 fix the "multi-line STO = multiple notifications" bug?
**CORRECTED (Sep 18 2026) - test did NOT actually prove EM2 helped.** User clarified after
the test: creating the Outbound Delivery MANUALLY via SAP UI (which is what the
STO-000110/P8D1-238 test above did) has ALWAYS correctly produced ONE delivery ID for a
multi-line STO, regardless of EM2 - this was already known-good behavior, not something
EM2 changed. So the STO-000110 test below did NOT isolate the EM2 variable at all - it
just confirmed the already-known-good manual-UI path, which doesn't tell us anything new.

The ORIGINAL bug the user described (multi-line STO producing MULTIPLE separate
notifications) must have happened through some OTHER mechanism - NOT the manual
"Create Outbound Delivery" UI click. Likely candidate: some AUTOMATIC/background SAP
process (e.g. a scheduled Delivery Due List / MDRO-type mass run) rather than a manual
click - but this is NOT YET CONFIRMED. NEED TO ASK USER: what exactly was the original
scenario/mechanism where multi-line STOs produced multiple separate notifications? Was it
via an automatic background job, or something our own app's code triggered, or something
else? Do not assume EM2 fixes this until the actual original mechanism is identified and
re-tested against it specifically.

Test data below (STO-000110/P8D1-238) is still valid factual data, just doesn't prove the
EM2 hypothesis - keeping it for reference in case the original bug's mechanism turns out
to be the same delivery-request-to-delivery conversion step after all.

Test: created a real multi-line test STO **STO-000110** (P8 -> P1, items 09200726-01 +
092018-PF, both from P8-RM) via the app's real API. SAP order 32293, Delivery Request
**61097**. User then did the normal "Create Outbound Delivery" (with Release) step in SAP
UI on this delivery request (Release Outbound Delivery is still set to Manually on EM2,
so this UI step was still needed). Result: **ONE** Outbound Delivery, **P8D1-238**, was
created - confirmed via read-only lookup to contain BOTH line items. But per user's
correction above, this is NOT evidence of an EM2 effect - manual UI creation always did
this correctly regardless of EM2.

## STO receiving via SOAP - TESTED, CONFIRMED NOT VIABLE (Sep 18 2026)
User asked to test the full loop: find an STO's real Inbound Delivery Notification ID and
attempt to release/post it via our new SOAP service (not Playwright, not the already-dead
OData `InboundDeliveryPGRBackground`).

1. Read-only lookup (via the ALREADY-EXISTING, unused `sap_inbound_delivery_client.py` ->
   `SAPInboundDeliveryClient.find_delivery_by_id()`, an OData GET, safe): found real
   candidate **STO-000102** (ship_to=P8, GI posted, never received) -> Outbound Delivery
   **P1D1-547**, confirmed genuinely un-received in SAP with 4 real items (A34212 2 EA,
   G12LW 2 EA, G12FW 2 EA, G12NUT 5 EA).
2. Note found while investigating: `sap_inbound_delivery_client.py` (built Aug 28 2026,
   NEVER wired into server.py/inbound_receipt_service.py - dead code, comment-only
   reference) already tried the more obvious OData action `InboundDeliveryPGRBackground`
   and got it OFFICIALLY CONFIRMED DISABLED by SAP (KBA 3583076: "Inability to Post Goods
   Receipt with Actual Quantities via OData API in Inbound Delivery Processing") - this is
   WHY Playwright was built in the first place. Did NOT retry this (would just repeat an
   already-conclusively-failed test).
3. Tried our SESSION's SOAP service instead (genuinely different mechanism) with a
   check-only call: `actionCode="02"` (Change), `DeliveryNotificationID=P1D1-547`,
   `releaseDocumentIndicator="true"`, no items/vendor (since none should be needed for an
   update to an existing doc). SAP rejected cleanly: **"Action Change not supported within
   IDN P1D1-547"** (severity 3, real validation error - not a schema mistake, SAP
   correctly recognized the ID as a real IDN, just rejects modifying it this way).

**Conclusion: this specific SOAP service (`StandardInboundDeliveryNotificationBundle...`)
cannot release/post an EXISTING STO-originated Inbound Delivery Notification** - it only
supports creating brand-new ones from a Purchase Order reference (the vendor-GRN use
case). STO receiving via direct API remains UNSOLVED - all 3 known avenues are now
confirmed dead (Playwright=stopped by policy, OData PGRBackground=disabled by SAP,
SOAP Bundle Create/Change=rejects existing STO IDNs). Would need genuinely new research
(a different SAP service/BO entirely, or working with SAP support on why PGRBackground
is disabled) before attempting anything further here. Vendor GRN automation (the original
breakthrough) is UNAFFECTED by this - remains the viable, provable path forward.

## Automatic Release investigation via "Outbound Delivery Release Run" (Sep 18 2026) - INCONCLUSIVE, PAUSED
Found the right work center: **"Outbound Logistics" -> Automated Actions -> "Outbound
Delivery Release Run"** (NOT "Outbound Logistics Control" -> "Confirmation Update Runs",
which is a different, unrelated ATP-refresh run - ruled out via web research). This is a
Mass Data Run (MDR) that must be explicitly scheduled - SAP ByDesign automatic outbound
processing does NOT happen on its own without one; the Logistics Model's "Automatic"
release setting only controls behavior WHEN a run executes, not whether one ever runs.

Created run "R1ODC" (Ship-from Site=P8, "Shipment/Delivery Date" left blank), Scheduled ->
Start Immediately. Job finished cleanly (Application Log 1011295, 0 errors), reporting
"1 outbound deliveries released - outbound delivery P8D1-205 released". BUT this turned
out to be a red herring: P8D1-205 is tied to **STO-000067**, an OLD STO already fully
`gi_status: posted` in our DB from before this session - NOT one of the 4 fresh test STOs
created during this investigation (STO-000109/110/111/112, Delivery Requests 61095/61097
/61098/61410). Confirmed via a fresh re-check: Delivery Request **61098 is still stuck**
(no Outbound Delivery), even after this run completed successfully.

**Conclusion: still unresolved/inconclusive.** The run mechanism exists and genuinely
works (it did release something), but our specific test STOs' Delivery Requests aren't
being picked up by it for reasons not yet understood (possibly an ATP/availability
confirmation prerequisite, or the blank "Shipment/Delivery Date" filter excluding them,
or something else). Also unexplained: why an ALREADY gi_status=posted STO's delivery
would need releasing again today - possibly our own app's status tracking and the SAP
document's actual release state can be independent/out of sync from each other.

**STATUS: PAUSED** after extensive back-and-forth (4 real test STOs created, 2 Logistics
Model config toggles flipped live on EM2, 1 real MDR run created and executed) without a
clean, conclusive result. Real, confirmed, useful findings from this whole thread remain:
(1) EM1 unlocks the vendor-GRN SOAP breakthrough (solid, proven, actionable now);
(2) the Outbound Delivery Release Run mechanism and location are now documented for any
future attempt; (3) the "multi-line STO = multiple notifications" bug's real mechanism is
still not identified (ruled out: manual UI creation always worked fine regardless of EM2;
confirmed: it happens via "an automatic/background process" per user - likely THIS same
Release Run mechanism, but not proven since our test STOs never got picked up by it).
NEXT SESSION should decide whether to keep digging here (diminishing returns so far) or
prioritize the already-proven vendor-GRN build-out instead.
