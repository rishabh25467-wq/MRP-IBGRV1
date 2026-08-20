# SAP Business ByDesign Integrations - Full Reference

Tenant: `my431827.businessbydesign.cloud.sap`. All credentials in `/app/backend/.env`.
Two integration styles are used throughout this app:
- **SOAP web services** (standard SAP ByDesign A2X/A1S services, via Communication Arrangements) - used for BOM reads, Production Proposal/Lot/WIP writes.
- **Custom OData services** (built by your SAP admin via the Key User "OData Service Builder", exposing specific Business Objects/fields/actions not available as standard APIs) - used for Production Model/Order lookups, Net Weight, Safety Stock/Lead Time, Standard Cost, On-Hand Inventory.

---

## 1. Reading BOM data (`sap_soap_client.py`)
- **Service**: `QueryProductionBillofMaterialsIn` (SOAP), operation `QueryProductionBillOfMaterialByElementsRequest`.
- **Endpoint env var**: `SAP_SOAP_ENDPOINT`. Auth: `SAP_SOAP_USERNAME`/`SAP_SOAP_PASSWORD` (technical user `_EMERGENTBOM`).
- **What it does**: given a BOM ID (e.g. `P26584_2`) or a bare product ID, fetches that BOM's header + all item groups/items (product ID, quantity, UoM, Engineering Change Order, active flag). Recursively explodes sub-BOMs level-by-level (BFS, up to 6 levels deep, batched requests up to 60 products/call) to build the full multi-level tree used by BOM Explorer and Purchasing Plan.
- **Key logic**: picks the current/Consistent BOM revision when multiple exist; detects genuine parallel "alternate" BOMs (different recipe, same output) vs simple revision history; per-line-item Engineering Change Order resolution picks the truly current input product using `EngineeringChangeOrderValidFromDate`.
- **No write capability** - this service is read-only by design.

## 2. Direct Material ID -> UUID lookup (`sap_material_client.py`)
- **Service**: `QueryMaterialIn`, operation `MaterialByElementsQuery_sync`.
- **Endpoint env var**: `SAP_SOAP_MATERIAL_ENDPOINT`.
- **What it does**: resolves a Material's business ID directly to its internal `UUID` - the ONLY way to get a UUID for a material that has no BOM relationship anywhere (not a BOM root, not anyone's ingredient). Also pulls the Material's Attachment Documents (drawing external link + free-text comments/ECR notes).
- **Used by**: Inventory page's "Resolve Missing Values" deep backfill (closing the Standard Cost coverage gap for un-explored materials).

## 3. Material Net Weight / Surface Area - read + write (`sap_material_physical_client.py`)
- **Service**: custom OData `materialgeneralinfo` (Business Object `Material`, 2 custom Key User Tool fields: `ItemNetWeight1`, `SurfaceAreaSqInch`, each with a `content_KUT` numeric value and `unitCode_KUT` unit).
- **Endpoint env var**: `SAP_MATERIAL_GENERALINFO_ODATA_URL`. Auth: `SAP_ODATA_USERNAME`/`SAP_ODATA_PASSWORD` (business user, NOT the SOAP technical user - custom OData services require a real Business User).
- **Read**: `GET .../MaterialCollection?$filter=InternalID eq '{id}'` returns Net Weight/Surface Area if populated.
- **Write**: PATCH with CSRF token fetch first (`x-csrf-token: fetch`), then PATCH on the same ObjectID.
- **Used by**: Production Confirmation's scrap calculation (Gross Weight from BOM - Net Weight = scrap/unit), and the Admin "Push Net Weight" tool.

## 4. Production Model ("Source of Supply") lookup (`sap_production_model_client.py`)
- **Service**: custom OData `productionmodelemergent` (`ProductionModelCollection` -> `ReleasedPlanningProductionModel` -> `SourceOfSupplyLogisticRelationship`, plus `ProductionModelSupplyPlanningArea` / `SupplyPlanningAreaCollection`).
- **Endpoint env var**: `SAP_ODATA_PRODUCTION_MODEL_BASE_URL`.
- **What it does**: given a material's UUID, returns every valid (Production Model, Site) combination it can be produced under, with the `logistic_relationship_uuid` needed to force a specific model at Proposal creation. A model determines its site - not the other way around.
- **Read-only.**

## 5. Creating a Production Proposal (`sap_production_proposal_client.py`)
- **Service**: `ManageProductionProposalIn`, operation `CreateBundleRequest` (SOAP).
- **Endpoint env var**: `SAP_SOAP_PRODUCTION_PROPOSAL_ENDPOINT`.
- **What it does**: creates a Production Proposal (Material, Site/Supply Planning Area, Quantity+UnitCode, Availability Date). This is SAP's ONLY supported way to create a new production order via web service - there is no direct "create order" API; SAP's planning run (or our own trigger, see #6) later converts the Proposal into a Production Order.
- **Known gotcha**: `Quantity`/`QuantityTypeCode` use whatever `unit_code` string is passed - NOT validated against the material's actual base UoM before sending (flagged, pending a SAP-side fix, see open item below).

## 6. Creating a Proposal with an explicit Production Model + Releasing an Order (`sap_production_order_release_client.py`)
- **Service**: custom OData `productionorderemergent` (`ProductionOrderCollection`, custom `Release` action, custom `ProductionPlanningOrderCreate` Function Import, custom field `Z_ProductionProposalIDcontent_SDK`, standard field `LifeCycleStatusCode`).
- **Endpoint env var**: `SAP_ODATA_PRODUCTION_ORDER_RELEASE_BASE_URL`.
- **What it does**:
  - `create_with_source_of_supply()`: creates a Proposal while FORCING a specific Production Model (`SourceOfSupplyLogisticRelationshipUUID`) - only possible at creation time, SAP rejects setting this on an existing Proposal via PATCH. Note: `MainMaterialOutputQuantity` must be sent with a trailing `m` (Edm.Decimal literal suffix) or SAP returns "Malformed URI literal syntax".
  - `release_order()`: fires the standard `Release` action (CSRF token + POST), optionally verifying the real `LifeCycleStatusCode` afterward instead of trusting a bare HTTP 200.
  - `list_ids_by_status()`: globally lists all Orders stuck "In Preparation" (status code 1) - used by the background job to self-heal orders that never got picked up by the old Lot-polling approach.
  - `tag_with_proposal_id()`: best-effort PATCH writing the source Proposal ID onto the new Order's `Z_ProductionProposalID` custom field, for instant future lookups + visibility in SAP's native UI.
- **Life Cycle Status codes**: 1=In Preparation, 2=Released, 3=Started, 4=Finished, 5=Closed, 6=Canceled.

## 7. Production Lot / Task Confirmation (`sap_production_lot_client.py`)
- **Services**: `QueryProductionLotISIIn` (read) + `ManageProductionLotsIn` (write), both SOAP.
- **Endpoint env vars**: `SAP_SOAP_PRODUCTION_LOT_QUERY_ENDPOINT` / `SAP_SOAP_PRODUCTION_LOT_MANAGE_ENDPOINT`.
- **Read** (`find_open_lots`/`find_lot_by_id`): lists open Production Lots with their Confirmation Groups, Production Tasks, Reporting Points, AND already-planned Output Products (main output + by-products like `IRON-SCR`) each with `MaterialOutputUUID`/planned/open quantity.
- **Write**:
  - `confirm_reporting_point()`: posts Confirmed Quantity / Confirmed Scrap (manually-entered rejected qty) / Deviation Reason / Finished flag against ONE Reporting Point. **Deliberately never sends component (MaterialInput) quantities** - SAP's own backflush auto-consumes BOM components from the confirmed output. If `confirmation_finished=true`, automatically follows up with `finish_task()`.
  - `finish_task()`: standalone call that transitions the Production Task's own life cycle (In Process -> Finished) - separate request, no ReportingPoint/Material nodes allowed in it per SAP's API contract.
  - `confirm_material_output()`: confirms the quantity of an EXISTING by-product output line (`ActionCode="02"`, Change) - used to post our own computed physical-scrap weight instead of SAP's own (often stale) planned quantity. **Must run BEFORE `finish_task()`** - once a lot is Finished, SAP locks it and rejects further MaterialOutput writes.

## 8. WIP Clearing Run (`sap_wip_clearing_client.py`)
- **Service**: `AccountingWIPClearingRun`, operation `CreateWipRequest` (SOAP).
- **Endpoint env var**: `SAP_SOAP_WIP_CLEARING_ENDPOINT`.
- **What it does**: triggers a real WIP Clearing Run for one Production Lot (zeroes its work-in-process inventory for period-end reporting) - fired automatically whenever a task is marked Finished. Requires Company/Set of Books per site (hardcoded: P1/P8 -> RI/RSOB, everything else -> RT/RDOB) and the fiscal calendar (Apr-Mar).

## 9. Safety Stock / Procurement Lead Time - read + write (`sap_planning_client.py`)
- **Service**: custom OData `materialltmsl` (`MaterialSupplyPlanningProcessInformationCollection`).
- **Endpoint env var**: (base URL in .env, service `materialltmsl`). One row per Material x Supply Planning Area (site).
- **Read**: `get_planning_data()` - batched, `MaterialUUID eq guid'...'` OR'd filters.
- **Write**: `push_planning_data()` - CSRF + PATCH per row, 4-attempt retry on SAP's transient "Locking object not possible" error. Pushes to ALL Supply Planning Areas for a material at once.
- **Used by**: Admin page's per-row and bulk "Push to SAP" for MSL/Lead Time.

## 10. Standard Cost / Valuation (`sap_valuation_client.py`)
- **Service**: custom OData exposing `MaterialValuationDataValuationLevelCollection` + `MaterialValuationDataValuationPriceCollection`.
- **Chain**: Product UUID -> ValuationLevel (per company/site) -> ValuationPrice (date-ranged, picks the currently-valid, preferring non-zero if multiple sites' levels exist). Read-only.
- **Used by**: BOM Explorer's Total BOM Cost, Purchasing Plan's value columns, Inventory page's Unit Cost/Total Value.

## 11. On-Hand Inventory (`sap_inventory_client.py`)
- **Service**: custom Analytics OData report on the "On-Hand Inventory" (SCMINVV02) data source.
- **Critical quirk**: MUST fetch full, un-`$select`'d rows - selecting only a subset of fields makes `CMATERIAL_UUID` return an internal numeric surrogate key instead of the real Material ID. Paginates via `$top=5000`/`$skip`. Read-only.
- **Used by**: Purchasing Plan's inventory netting, Inventory page's cached snapshot (refreshed every 2h).

## 12. Creating new Materials (`sap_material_create_client.py`)
- **Service**: `ManageMaterialIn` (standard "Manage Materials" scenario), operation `MaintainBundle_V1` (create, actionCode 01) / delete (actionCode 03).
- **Endpoint env var**: `SAP_SOAP_MATERIAL_MANAGE_ENDPOINT`.
- **UI**: `/admin/create-material` (passcode-gated + `admin_create_material` permission).
- **IMPORTANT**: `CheckMaintainBundle_V1` (SAP's documented dry-run) does NOT dry-run on this tenant - it executes a real create regardless of the SOAPAction sent. Never attempt to use it for validation; the backend instead checks via `QueryMaterialIn` that the ID is free before creating. Delete (actionCode 03) works for cleaning up an unused/"In Preparation" material.

## Not BOM/Production related (other app pages, listed for completeness)
- `sap_gsa_client.py`, `sap_cost_estimate_client.py`, `sap_price_spec_client.py`, `sap_supplier_client.py`, `sap_supplier_invoice_client.py` - power the Price Explorer / supplier-facing pages, unrelated to BOM/Production.

## Open items / known gaps
- **UoM auto-lock - DONE (2026-08-20)**: SAP admin exposed `Common.BaseMeasureUnitCode` on `materialgeneralinfo` (activated). The Create Production Order form now auto-fills + locks UoM from this field the same way Site locks from the Production Model, falling back to EA-default + warning only if SAP has no value for that material.
- **Live SAP Inventory OData 500 error** (blocked on SAP Basis team) - app falls back to the cached DB snapshot.
- **Missing By-Product Creation on a Production Model** - deprioritized by user (existing model by-product rows are sufficient for current use).
