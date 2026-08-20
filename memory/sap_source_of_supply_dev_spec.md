# SAP Developer Spec: Programmatic "Source of Supply" (Production Model) Selection

## Business need
When a material has multiple valid Production Models (Sources of Supply), our app needs to let the
user pick one when creating a Production Proposal from "Create Production Order" - equivalent to
SAP's own "Change Source of Supply" button in Interactive Planning (Product Planning Details ->
Supply and Demand List, applied on a "Production Proposal (Firm)" row).

## What already exists (built by us, live today)
- `productionproposalemergent` custom OData service - entity `ProductionPlanningOrder`
  (currently exposes only ObjectID/ID/UUID) + `Release` action (POST, param `ObjectID`). Used to
  convert a Proposal into a Production Request.
- `productionrequestemergent` custom OData service - entity `ProductionRequest` (has a read-only
  field `ReleasedExecutionProductionModelUUID`, confirmed populated with a real GUID once SAP
  auto-picks a model) + entity `ProductionRequestProductionSegment` + `RELEASE` action. Used to
  convert a Request into an actual Production Order.

## UPDATE (current, confirmed) - the real fields already exist on the BO, no custom action needed
User located them live in Cloud Application Studio's OData Editor: on
`productionproposalemergent` -> Entity Types -> `ProductionPlanningOrder` -> **Root** node, the
underlying Business Object already exposes these standard fields (currently NOT selected/exposed in
the service):
- `SourceOfSupplyLogisticRelationshipUUID` - the actual Source of Supply reference (GUID)
- `SourceOfSupplyFixedIndicator` - boolean; per SAP's own docs, a manually-chosen source of supply
  must be marked "Fixed" or automatic planning can just recalculate and override it
- `SourceOfSupplyExplosionDate` - present too, likely read-only/derived - include if selectable, not
  required

### Steps for the developer
1. In the same OData Editor screen (screenshot already taken), check the **Select** checkbox for
   `SourceOfSupplyLogisticRelationshipUUID` and `SourceOfSupplyFixedIndicator`.
2. Confirm both inherit Create/Update = true from the `ProductionPlanningOrder` entity (already
   ticked in the right-hand panel for the entity as a whole) - if the tool exposes a per-field
   creatable/updatable override, make sure both are enabled specifically.
3. Save, then **Activate** the service (top-left button).
4. Re-test: `GET https://my431827.businessbydesign.cloud.sap/sap/byd/odata/cust/v1/productionproposalemergent/$metadata`
   should now list both fields on `ProductionPlanningOrder` with `sap:creatable="true"
   sap:updatable="true"`.
5. **Open question for the developer/Basis team**: what value goes INTO
   `SourceOfSupplyLogisticRelationshipUUID` for "use Production Model X"? This looks like it
   references a "Fixed Source of Supply" / Logistic Relationship master-data record (the same
   underlying concept behind Quota Arrangements, just for in-house production instead of external
   procurement) rather than the Production Model ID directly. Please confirm:
   - Where these Logistic Relationship records are maintained for in-house production sources of
     supply (likely Sourcing work center, "Fixed Source of Supply" for internal production), and
   - Whether there's a way to read/list them per material (OData or SOAP) so we can map
     "Production Model X" -> its Logistic Relationship UUID before writing it here.
6. Once confirmed, report back the read API/location so we can finish wiring the picker.

## CONFIRMED LIVE (this session) - writes work
User activated the service with both fields selected. Verified live with real MERGE/PATCH calls
against a real Proposal (223822):
- `SourceOfSupplyFixedIndicator` - write succeeded (204), value persisted.
- `SourceOfSupplyLogisticRelationshipUUID` - write succeeded (204) with its own current value
  (safe no-op test), value persisted.
- Metadata still SHOWS `sap:updatable="false"` on these two fields (likely a stale/cosmetic
  metadata annotation, not an enforced restriction) - the collection-level `updatable="true"` is
  what actually governs it. Not blocking - writes work regardless of what `$metadata` displays.

**Still needed before the picker can be built**: a real alternate Logistic Relationship UUID to test
with (we've only ever seen the ONE default value SAP auto-assigns), and a way to look up/list the
valid alternates for a given material. This is the one remaining open item - see "Open question" in
the UPDATE section above (where these Fixed Source of Supply / Logistic Relationship records are
maintained, and whether there's a read API to list them per material).

## CONFIRMED via official BO Documentation (user downloaded it directly from SAP via the "Download Business Object Documentation" button)
The `ProductionPlanningOrder` BO's own `Create` action parameter list explicitly includes
`SourceOfSupplyLogisticRelationshipUUID` and `SourceOfSupplyExplosionDate` (alongside
`FixedIndicator`) - confirming this is a first-class, intended input at creation time in SAP's own
design, not a workaround. Our existing SOAP `create_proposal()` call may not expose this exact
parameter in its simplified WSDL (the SOAP interface uses its own curated field names, e.g.
`MaterialID`/`SUPPLY_PLANNING_AREA_ID`, which don't necessarily mirror every BO action parameter
1:1) - but that doesn't matter in practice: we already proved live that PATCHing
`SourceOfSupplyLogisticRelationshipUUID` + `SourceOfSupplyFixedIndicator` via the OData service
right after creation works (204, persisted) - that remains our path, no need to touch the working
SOAP client.

The doc also confirms "Logistic Relationship" is a genuinely separate referenced object (not just
an alias for the Production Model's own UUID - already proven distinct: Proposal's
`SourceOfSupplyLogisticRelationshipUUID` != Request's `ReleasedExecutionProductionModelUUID` for
the same order, different UUIDs). The doc does not define what that Logistic Relationship object
itself is or how to query it per material - still the one open gap.

### Next concrete step
Ask the developer to use the SAME "Download Business Object Documentation" button (visible in the
OData Editor screenshot, top of the Entity Types panel) on whatever BO represents **"Logistic
Relationship"** - search Repository Explorer for that exact name - or on the standard **"Production
Model"** BO (likely exposed via `ManageProdModelIn`/`ReadProductionModel`) and download ITS
documentation the same way. That should reveal either: a query to list Logistic Relationship
records per material, or a field on Production Model that itself carries/links to the Logistic
Relationship UUID we need. Upload that HTML doc the same way this one was and we can finish wiring
the picker.

## Once available, we will
1. On "Create Production Order", look up the available Production Models (and their Logistic
   Relationship UUIDs) for the entered material.
2. If more than one exists, show a picker in the UI before submitting.
3. Right after creating the Proposal, write the chosen `SourceOfSupplyLogisticRelationshipUUID`
   (and set `SourceOfSupplyFixedIndicator = true`), then continue the existing automated
   Proposal -> Request -> Order flow unchanged.

## Earlier investigation (superseded, kept for reference only)
Before the fields above were found, we assumed a custom BOPF action would be needed (matching how
`Release` was originally built) - e.g. a new Function Import like
`SETSOURCEOFSUPPLY(ObjectID, ProductionModelUUID)`, requiring Repository Explorer research into the
underlying Supply Planning BO's actions. That is NOT needed now that the native
`SourceOfSupplyLogisticRelationshipUUID`/`SourceOfSupplyFixedIndicator` fields have been found
directly on `ProductionPlanningOrder` - simply exposing them (steps above) should be sufficient.

We had also asked for `ManageProductionModelIn`/`QueryProductionModelIn` (SOAP) endpoint access to
list a material's available Production Models for the picker UI - that read-side request still
stands regardless of which write approach is used.
