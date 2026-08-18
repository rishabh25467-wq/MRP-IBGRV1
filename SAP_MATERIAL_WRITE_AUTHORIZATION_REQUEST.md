# SAP Authorization Request: Material Weight/Dimensions Write (ManageMaterialIn)

## Who this is for
Your SAP Business ByDesign Basis/Admin team (or whoever manages Communication
Arrangements and Business User roles in your SAP tenant).

## Background
The app's Admin page now has a "Weight & Dimensions" feature that lets users
enter a component's Net/Gross Weight, Net/Gross Volume, and Length/Width/
Height, then push those values into SAP's Material master "UoM
Characteristics" tab. Reading this data already works today (via the
already-authorized `QueryMaterialIn` service). Writing it needs a separate,
NEW SAP service: **`ManageMaterialIn`** (operation `MaintainBundle_V1`),
using the same technical user already used for Material lookups,
**`_EMERGENTBOM`**.

## What's failing right now
A live test call to the guessed endpoint for this service returned a
generic SOAP fault: `Web service processing error` (not the specific
"Authorization role missing" fault seen before with `QueryMaterialIn` prior
to its own arrangement being set up) - this means **no Communication
Arrangement/Scenario for "Manage Materials" exists in this tenant yet at
all**, so it needs to be created from scratch, not just extended.

**Technical details of the call:**
- **SOAP Service:** `ManageMaterialIn`
- **Operation:** `MaintainBundle_V1`
- **Endpoint pattern tried:** `https://<tenant>.businessbydesign.cloud.sap/sap/bc/srt/scs/sap/managematerialin`
- **Business user:** `_EMERGENTBOM`

## What we need done
1. Go to **Application and User Management → Communication Arrangements**.
2. Create a new Communication Arrangement using the inbound scenario
   **"Manage Materials"** (same self-service steps you've used before for
   "Production BOM Query", "materialquery", and others - no SAP support
   ticket needed for those).
3. Link it to the same technical/business user `_EMERGENTBOM`, and ensure
   its business role includes access to the `ManageMaterialIn` service /
   `MaintainBundle_V1` operation.
4. Once active, please share the exact SOAP endpoint URL shown in the
   arrangement (it may differ slightly from the guessed path above) so we
   can update our config if needed.

## How to confirm it's fixed
Once the arrangement/role is added, let us know - we will re-test the exact
same call from our side immediately (no redeploy needed) and confirm the
write goes through.

## Why this matters
Once working, users can enter a component's physical dimensions directly in
this app (which most materials in this tenant don't have populated in SAP
yet - live-confirmed 18 Aug 2026, only 2 of 11,092 materials have any value
set) and push them straight into SAP's Material master, instead of needing
someone with SAP UI access to enter them one by one on the "UoM
Characteristics" tab.
