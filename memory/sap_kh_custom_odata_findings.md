# SAP Custom OData Services (`kh*`) — Investigation Findings (Sep 22 2026)

User supplied `SAP_ByD_Custom_OData_Package.zip` containing 4 SAP-sample custom
OData service definitions (Cloud Application Studio/PDI exports, `cust`
namespace): `khinbounddelivery`, `khinbounddeliveryrequest`,
`khgoodsandactivityconfirmation`, `khgoodsandserviceacknowledgement`.

Base URL pattern: `https://my431827.businessbydesign.cloud.sap/sap/byd/odata/cust/v1/{service}`
Auth: same `SAP_ODATA_USERNAME`/`SAP_ODATA_PASSWORD`/`BYD_ODATA_VHOST` as our
existing `inboundstockemergent` service.

## Activation status (live-tested)
All 4 were initially "No implementation for service" (not deployed). The
user's SAP team activated all 4 during this session — activation happens
server-side (Business Configuration), nothing for us to do.

## 1. `khinbounddelivery` — CONFIRMED USEFUL for STO receipt status
Wraps `APDL_INBOUND_DELIVERY` — this is the **Inbound Delivery Notification**
(same doc our own `sap_inbound_delivery_client.py`/`inboundstockemergent`
service already reads/writes; same `ObjectID`s, confirmed identical
underlying SAP objects).

**Read side — solid win, safe, already differentiates real states:**
- `InboundDeliveryCollection`: `ReleaseStatusCode`, `ConsistencyStatusCode`,
  `DeliveryNoteStatusCode`, `DeliveryProcessingStatusCode`,
  `CancellationStatusCode` (all with `...Text` human-readable variants).
- Confirmed live: fully-received STO delivery (P1D1-570) shows
  `DeliveryProcessingStatusCode: Finished`, `ReleaseStatusCode: Released`,
  `DeliveryNoteStatusCode: Received`. Not-yet-received deliveries show
  `Not Started` / `Not Released` / `Advised`. **This genuinely answers the
  "receipt status" question from earlier in this session** — unlike
  `ManageCustomerRequirementIn`'s read (which only reflects outbound/GI
  fulfillment, always "Finished" regardless of receiving).

**Write side — DO NOT rely on this for real Goods Receipt (see incident below).**
Actions: `CreateWithReference`, `Release(ObjectID, TaskBasedIndicator)`,
`PostGoodsReceipt(ObjectID)`, `SetAsAdvised(ObjectID)`,
`AcknowledgeDeliveryNoteReceipt(ObjectID)`.

Discovered live sequencing requirement (not documented anywhere): actions
are disabled until prerequisites are met —
`AcknowledgeDeliveryNoteReceipt` (Advised→Received) must run before
`Release` becomes possible. Confirmed via live test on STO-000088
(`P8D1-231`): `Acknowledge` → `Release` succeeded, `DeliveryProcessingStatusCode`
flipped to `Finished` immediately (before any `PostGoodsReceipt` call).

**INCIDENT — real, live write action taken (with explicit user approval)
that did NOT produce a real Goods Receipt:**
- Site P1 is a **task-based warehouse**. SAP's own Document Flow (user
  screenshot) shows: `Inbound Delivery Notification (P8D1-231)` →
  `Warehouse Request (86913)` → `Warehouse Order (102103)` →
  **`Inbound Delivery (54031)`**. The last node is a DIFFERENT SAP
  business object from the Notification — confirmed via the Notification's
  own printed form (user-supplied `Download.xml`):
  ```xml
  <ConfirmedInboundDeliveryReference>
    <ID>54031</ID>
    <UUID>fa163e88-19fa-1fd1-add3-01fbacbd41b6</UUID>
    <TypeCode>24</TypeCode>
  </ConfirmedInboundDeliveryReference>
  ```
- Calling `Release` on the Notification only administratively closes the
  Notification and spawns the Warehouse Request/Order chain — it does
  **not** post real inventory. User independently confirmed in SAP UI:
  "fulfilled qty went as 0" after our test.
- Confirmed this UUID (`fa163e88-19fa-1fd1-add3-01fbacbd41b6`) is **not**
  present in any of the 4 `kh*` services (tried `UUID eq guid'...'` filter
  on all 4 EntitySets — zero matches everywhere). We have **no API access
  at all** to this second-stage `Inbound Delivery` (TypeCode 24) object.
- Also re-confirmed (Sep 22) the Sep 18 dead-end still holds:
  `sap_site_logistics_client.py` (`QuerySiteLogisticsTaskIn`) does not
  surface a task for this STO/site either (only 1 unrelated task exists
  site-wide for P1).
- **User's decision: leave STO-000088 as-is ("shows finished, I can live
  with it"), no rollback attempted.**

**What's needed for real STO receiving automation**: a new custom
OData/SOAP service exposing the second-stage `Inbound Delivery` object
(TypeCode 24, e.g. ID `54031`, the one that appears *after* Warehouse
Order in Document Flow) — completely separate ask from `khinbounddelivery`.
Give SAP Basis/PDI team the exact UUID/TypeCode above as a starting point
to identify the right BO.

## 2. `khinbounddeliveryrequest` — NOT useful for STOs, but big potential for vendor GRN
Wraps `APDL_INB_DELIVERY_REQ`. Read-only (`ItemQueryByElements`,
`InboundDeliveryRequestQueryByElements`, both GET). Query params are
`PurchaseOrderID`/`PurchaseOrderItemID` — string params must be
single-quoted in the URL (e.g. `PurchaseOrderID='29742'`) or SAP returns a
misleading 400 "Invalid function import parameter type".

- Zero matches for any STO's `sap_order_id` (STOs aren't tracked here —
  confirmed, this object is PO-specific, not STO/CustomerRequirement).
- **Real match for genuine vendor procurement POs** (tested 29535, 29510,
  29742 — all real, all matched).
- `Item.OrderFulfilmentProcessingStatusCode` + `ConfirmationItem` →
  `ConfirmationItemQuantity` (`QuantityRoleCode`: 23=Forwarded,
  24=**Fulfilled**, 17=Delivered, 139=Returned) gives genuine granular
  ground-truth quantities per PO line.
- **Found a real discrepancy**: our app's own history for PO 29742 claims
  "Fulfilled Quantity now matches Planned Quantity" (fully put away), but
  live SAP data via this service shows line 1 Forwarded=3.0/**Fulfilled=1.0**,
  line 2 Forwarded=2.0/**Fulfilled=0.0** — i.e. SAP's real ground truth
  disagrees with what our app believes. Worth deeper investigation before
  building on it (only checked 1 PO in-depth so far) — **flagged, not yet
  acted upon per user's instruction to keep focus on STO for now.**

## 3. `khgoodsandactivityconfirmation` — NOT currently useful
Wraps `APGAC_GA_CFM`. Live but data is stale (records dated Oct 2024,
`InventoryStockMovementStatusCode` blank on all sampled rows). A record
coincidentally sharing ID "54031" with our STO's real Inbound Delivery is
unrelated (different doc series, dated 2024). Not usable for current
STO/GRN activity as configured today.

## 4. `khgoodsandserviceacknowledgement` — NOT usable for our vendor GRN POs
Wraps `/SRMAP/LPURX_GSA`. Live, real status fields (`LifeCycleStatusCode`,
`ReleaseStatusCode`, `ApprovalStatusCode`, `GoodsReturnStatusCode`) but
tested against 5 real vendor PO numbers (29535, 29510, 29735, 29747,
29742) — **zero matches on all 5**. Sample GSA records reference a
different, much lower PO-number series (4074, 4091, 4100, 4406) — this
tenant uses GSA for a different business scenario (likely subcontracting),
not standard vendor procurement GRN. Not usable as-is.

## Summary table
| Service | Live? | Useful for STO? | Useful for vendor GRN? |
|---|---|---|---|
| `khinbounddelivery` | Yes | Status: yes. Write/PGR: no (see incident) | N/A (different doc) |
| `khinbounddeliveryrequest` | Yes | No (zero match) | Yes — real fulfillment ground truth, worth pursuing |
| `khgoodsandactivityconfirmation` | Yes (empty/stale) | No | No |
| `khgoodsandserviceacknowledgement` | Yes | N/A | No — wrong PO series |
