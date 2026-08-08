# SAP Setup Guide: Custom OData Service for Procurement Price Specification (Purchasing Info Records)

## Who this is for
Your SAP Business ByDesign Basis/Admin / Key User (whoever set up the
existing custom OData services `materialvaluationdata` and `materialltmsl`
for Standard Costs and MSL - this follows the exact same technique).

## Goal
Expose "who supplies Product X, at what price" (SAP calls this a
**Procurement Price Specification** - the ByD equivalent of a Purchasing
Info Record) as a **filterable** OData service, so our app can query it by
Product ID directly. The existing SOAP service (`ManageProcurementPriceSpecificIn`)
only supports lookup by internal UUID - confirmed by inspecting its actual
WSDL - so it cannot answer "find all suppliers for this part" on its own.
A custom OData service is the same trick already used successfully for
Standard Costs (`materialvaluationdata`) and MSL (`materialltmsl`).

## Step 1 - Confirm scoping
1. Go to **Business Configuration → Implementation Projects** → open your
   active project → **Edit Project Scope**.
2. In the Scoping step, under **General Business Data → Product and Service
   Pricing**, confirm **Purchase List Price** is selected/scoped. If not,
   add it and finish the scoping wizard.

## Step 2 - Get OData Services access
1. Go to **Application and User Management → Business Users**.
2. Open the business user who will build/own this service (can be your own
   admin account - this is a design-time step, not the runtime technical
   user).
3. **Edit → Access Rights** → assign the work center view
   **`ODATA_BYD_WOC_VIEW`** (OData Services) if not already present.

## Step 3 - Create the custom OData service
1. Go to the **OData Services** work center view (now visible after Step 2)
   → **Custom OData Services** → **New**.
2. Give it a name, e.g. `procurementpricespec` (lowercase, no spaces - this
   becomes part of the URL, same convention as `materialvaluationdata`).
3. Click **Select Business Object** and search for **"Procurement Price
   Specification"** (or try "Price Specification" / "List Price" if the
   exact name doesn't appear - the OData Modeler only shows Business
   Objects/nodes that are released in SAP's Public Solution Model, same
   restriction that applies to every other object).
4. **Important - please check and tell us what you find**: this Business
   Object identifies the Supplier and Product via a generic
   "PropertyValuation" node (technical condition-technique fields
   `CND_SUPPL_ID` / `CND_PRODUCT_ID`), not simple flat `SupplierID`/
   `ProductID` fields - this is different from the objects you've exposed
   before (Material, MSL) which have plain fields. When you open the node
   tree in the modeler:
   - If you see `PropertyValuation` (or similarly named) as a sub-node with
     an ID/value field, add that node and its fields.
   - If the modeler does NOT let you select clean, filterable Product
     ID/Supplier ID fields at all (the node isn't exposed this way), this
     approach hits a dead end at the SAP platform level - let us know and
     we'll fall back to the write-only sync approach instead.
5. Select these fields for the main node (add more if visible/useful):
   - `UUID` (technical ID)
   - `ChangeStateID`
   - Rate/price fields (e.g. `Rate.DecimalValue`, `Rate.CurrencyCode`)
   - `ValidityPeriod` (start/end dates)
   - The `PropertyValuation` sub-node fields for Supplier ID and Product ID
     (per point 4 above)
6. For each field you want to search/filter by (especially the Product ID
   and Supplier ID fields), check the **"Filterable"** checkbox/property in
   the field properties panel.
7. **Save**, then **Activate**. Activation generates the metadata/service
   URL, shown on the service's overview screen - it will look like:
   `https://my431827.businessbydesign.cloud.sap/sap/byd/odata/cust/v1/procurementpricespec/`

## Step 4 - Create the Communication Arrangement
1. Go to **Application and User Management → Communication Arrangements** →
   **New**.
2. Communication Scenario: **"OData Services for Business Objects"** (the
   same one used for `materialvaluationdata`/`materialltmsl`).
3. Select your new custom service (`procurementpricespec`) in the
   arrangement's service selection.
4. Assign the same business user already used for Standard Costs/MSL
   (`UNEECOPSTEAM`) - custom OData services are consumed by business users,
   not the `_EMERGENTBOM` technical SOAP user.
5. Save/activate.

## Step 5 - Send us the result
Please share:
- The final activated OData service URL (from Step 3.7)
- Confirmation of which fields ended up filterable (especially Product ID)
- If Step 4 (finding Product ID/Supplier ID as clean fields) hit a dead
  end, tell us that too - we have a fallback plan (write-only sync of new
  assignments you create in our app, into SAP, via the working
  `MaintainBundle` operation) ready to build instead.

## Why this matters
Once this works, the Suppliers page can show real "who supplies Product X
at what price" data pulled live from SAP (matching what your purchasing
team already sees in the SAP UI), instead of requiring re-entry in our app.
