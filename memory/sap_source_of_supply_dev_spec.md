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

## Once both are available
We will:
1. On "Create Production Order", look up available Production Models for the entered material.
2. If more than one exists, show a picker in the UI before submitting.
3. Write the chosen `ProductionModelUUID` via whichever action/field you expose (Option A or B
   above), then continue the existing automated Proposal -> Request -> Order flow unchanged.
