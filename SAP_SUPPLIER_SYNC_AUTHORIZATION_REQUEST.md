# SAP Authorization Request: Supplier List Read (QuerySupplierIn)

## Who this is for
Your SAP Business ByDesign Basis/Admin team (or whoever manages Communication
Arrangements and Business User roles in your SAP tenant).

## Background
The new "Suppliers" page in the app needs to read the live Supplier master
list from SAP (Internal ID, Name, Contact, Email, Phone) so users pick real
SAP suppliers instead of typing them manually. This uses the standard SAP
service **`QuerySupplierIn`** via the same technical user already used for
BOM/Material lookups, **`_EMERGENTBOM`**.

## What's failing right now
A live test call to a guessed endpoint for this service returned a generic
SOAP processing fault (not the specific "Authorization role missing" fault
we saw before with `QueryMaterialIn` prior to ITS setup) - this most likely
means **no Communication Arrangement/Scenario for "Query Supplier" exists
in this tenant yet at all** (as opposed to an existing one just missing a
role), so it needs to be created from scratch, not just extended.

**Technical details of the call:**
- **SOAP Service:** `QuerySupplierIn`
- **Operation:** `SupplierByElementsQuery_sync` (SOAP action `FindByElements`)
- **Endpoint pattern:** `https://<tenant>.businessbydesign.cloud.sap/sap/bc/srt/scs/sap/querysupplierin`
- **Business user:** `_EMERGENTBOM`

## What we need done
1. Go to **Application and User Management → Communication Arrangements**.
2. Check whether a Communication Arrangement for a **"Query Supplier"** (or
   similarly-named "Business Partner Data - Supplier") scenario exists.
3. If it does NOT exist, create/activate it (same self-service steps you
   used before for "Production BOM Query" and "materialquery" - no SAP
   support ticket needed for those).
4. Link it to the same technical/business user `_EMERGENTBOM`, and ensure
   its business role includes access to the `QuerySupplierIn` service /
   `FindByElements` operation.
5. Once active, please share the exact SOAP endpoint URL shown in the
   arrangement (it may differ slightly from the guessed path above) so we
   can update our config if needed.

## How to confirm it's fixed
Once the arrangement/role is added, let us know — we will re-test the exact
same call from our side immediately (no redeploy needed) and confirm data
comes back.

## Why this matters
Once working, the Suppliers page's "Sync from SAP" button will pull your
real supplier list automatically instead of requiring manual entry, and
keep supplier names/contact details up to date going forward. Note: per-part
Quota %/Lead Time/Price assignments remain LOCAL-only in this app (SAP ByD
does not currently expose a supported write-back API for that data - see
`SAP_INTEGRATION_GUIDE.md` for details), so this request is specifically
about the read-only Supplier master list.
