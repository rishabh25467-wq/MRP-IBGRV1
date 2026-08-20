# SAP Developer Spec: Programmatic "Source of Supply" (Production Model) Selection

## Business need
When a material has multiple valid Production Models (Sources of Supply), our app needs to let the
user pick one when creating a Production Proposal from "Create Production Order" - equivalent to
SAP's own "Change Source of Supply" button in Interactive Planning (Product Planning Details ->
Supply and Demand List).

## What already exists (built by us, live today)
- `productionproposalemergent` custom OData service - entity `ProductionPlanningOrder`
  (ObjectID/ID/UUID only) + `Release` action (POST, param `ObjectID`). Used to convert a Proposal
  into a Production Request.
- `productionrequestemergent` custom OData service - entity `ProductionRequest` (has a read-only
  field `ReleasedExecutionProductionModelUUID`, confirmed populated with a real GUID once SAP
  auto-picks a model) + entity `ProductionRequestProductionSegment` + `RELEASE` action (POST, param
  `ObjectID`). Used to convert a Request into an actual Production Order.

## The gap
Neither service currently exposes a WRITABLE field for choosing the Production Model:
- `ProductionPlanningOrder` (Proposal): no production-model field at all today.
- `ProductionRequest`: has `ReleasedExecutionProductionModelUUID` but it's `sap:updatable="false"`.

SAP's own UI applies "Change Source of Supply" at the **Proposal** stage (per screenshot: action
enabled on a "Production Proposal (Firm)" row), so that is the preferred attachment point - but
please confirm against the actual underlying Business Object (likely under Supply Chain
Planning/Production Proposal BO) which node truly owns this decision; the Request-level field may
just be a projection of a choice already made earlier at the Proposal.

## What we're asking for (pick whichever the BO structure actually supports)
**Option A (preferred, matches SAP UI behavior)**: Extend `productionproposalemergent` with either:
- a new extension field, e.g. `Z_SourceOfSupplyProductionModelUUIDcontent_SDK` (`Edm.Guid`,
  `sap:creatable="true"` and/or `sap:updatable="true"`) on `ProductionPlanningOrder`, settable via a
  PUT/MERGE before calling `Release`, OR
- a new custom Function Import, e.g. `SETSOURCEOFSUPPLY(ObjectID: Edm.String, ProductionModelUUID:
  Edm.Guid)` on `ProductionPlanningOrderCollection`, mirroring how `Release` was already built.

**Option B (fallback)**: Make `ReleasedExecutionProductionModelUUID` on `productionrequestemergent`'s
`ProductionRequest` entity updatable, or add an equivalent
`SETPRODUCTIONMODEL(ObjectID, ProductionModelUUID)` Function Import on that service instead, called
after the Proposal's `Release` step (which creates the Request) and before the Segment's `RELEASE`
step (which creates the real Order).

Either option is fine from our side - whichever matches how the BO actually enforces this choice
internally. Please just confirm which one you implement so we wire the write into the right step of
our automation (`_run_create_and_release_job` in `/app/backend/server.py`).

## Also needed: a way to list the available Production Models for a material
We need a read API to show the user their options before they pick one. SAP's standard service for
this is `ManageProductionModelIn` / `QueryProductionModelIn` (SOAP), operation `ReadProductionModel`
or `FindByElements` filtered by material ID. We don't currently have a WSDL/endpoint or a
communication arrangement scoped for this service - please provide:
- The endpoint URL for `ManageProductionModelIn` (or whichever service exposes
  `QueryProductionModelIn`/`ReadProductionModel`) in this tenant
- Confirm the `UNEECOPSTEAM` business user (already used for our other OData actions) has read
  access, or advise which technical/business user we should use

## Step-by-step for your developer (Option A - Proposal-level, preferred)

This follows the exact same pattern already used to build the existing `Release` action on
`productionproposalemergent`, so your developer should recognize the workflow. Done in SAP Cloud
Application Studio (PDI) against this tenant.

1. **Find the real internal action first (Repository Explorer)**
   - Open Repository Explorer, search for the standard Production Proposal / Supply Planning BO
     (likely under `AP.SupplyChainPlanning` or `AP.PSM.SupplyPlanning` namespace - node commonly
     named `ProductionProposal` or `SupplyPlanningExecutionOrder`).
   - Look for an existing BOPF action/method equivalent to "Change Source of Supply" - it's a
     standard UI button, so a corresponding action node method should exist (search action names
     containing "SourceOfSupply" or "ProductionModel"). This confirms the exact input it expects
     (almost certainly `ObjectID` + `ProductionModelUUID`, possibly also a `SupplyPlanningAreaID`).
   - This step is the main unknown - if no such action exists on the standard BO (only reachable via
     UI event, not BOPF action), fall back to Option B (below) or flag back to us so we can adjust.

2. **Extend the existing custom OData service project** (same PDI project as `Release`)
   - Add either:
     - a new **extension field** on `ProductionPlanningOrder`, e.g.
       `Z_SourceOfSupplyProductionModelUUID`, type GUID, and in its "before-save"/determination
       logic call the BOPF action found in step 1 with the field's new value; or
     - a new **custom Function Import**, e.g. `SETSOURCEOFSUPPLY`, with input parameters
       `ObjectID (Edm.String)` and `ProductionModelUUID (Edm.Guid)`, whose ABSL implementation calls
       the same BOPF action directly (mirrors how `Release` already wraps SAP's release logic).
   - Function Import is usually simpler/cleaner here since this is a one-shot command, not a
     persisted field - recommend this unless the field approach is already the established pattern
     in this PDI project.

3. **Mark it correctly in the OData Service Definition editor**
   - If a field: set `Creatable`/`Updatable` = true for `Z_SourceOfSupplyProductionModelUUID` on
     `ProductionPlanningOrderCollection`.
   - If a Function Import: `HttpMethod = POST`, `EntitySet = ProductionPlanningOrderCollection`,
     `ReturnType = cust.ProductionPlanningOrder`, parameters as in step 2.

4. **Assign to the same Communication Scenario / Business User** already used for `Release`
   (`UNEECOPSTEAM`) - no new communication arrangement needed if it's added to the same service.

5. **Activate and redeploy** the OData service (Studio -> Activate, then re-scope/redeploy the
   communication arrangement if the service definition itself was changed, not just its content).

6. **Test directly** against a real Proposal ObjectID before handing back to us, e.g.:
   ```
   POST https://my431827.businessbydesign.cloud.sap/sap/byd/odata/cust/v1/productionproposalemergent/SETSOURCEOFSUPPLY
   ?ObjectID='<Proposal ObjectID>'&ProductionModelUUID=guid'<model uuid>'
   ```
   (Basic Auth with `UNEECOPSTEAM`, `X-CSRF-Token` fetched first same as the existing `Release` calls.)
   Confirm the Proposal's resulting Production Model actually changes (check the same field/behavior
   your Interactive Planning UI's "Change Source of Supply" produces).

7. **Report back to us**: the exact field or Function Import name that ended up working, and
   whether it needs to be called before or after the existing `Release` call in the flow.

## Once both are available
We will:
1. On "Create Production Order", look up available Production Models for the entered material.
2. If more than one exists, show a picker in the UI before submitting.
3. Write the chosen `ProductionModelUUID` via whichever action/field you expose (Option A or B
   above), then continue the existing automated Proposal -> Request -> Order flow unchanged.
