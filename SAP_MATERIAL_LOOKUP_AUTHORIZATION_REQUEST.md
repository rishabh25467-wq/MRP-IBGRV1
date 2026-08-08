# SAP Authorization Request: Material Lookup (QueryMaterialIn)

## Who this is for
Your SAP Business ByDesign Basis/Admin team (or whoever manages Communication
Arrangements and Business User roles in your SAP tenant).

## Background
The SAP BOM Explorer app uses a dedicated SAP Business ByDesign technical
user, **`_EMERGENTBOM`**, to pull Bill of Materials data via SOAP. This same
user is already used successfully by the QMS app to look up material data,
so it already has *some* material-related access.

We now need this same user to also be authorized for one additional,
specific SAP standard service: **`QueryMaterialIn`**. This service resolves
a Material ID (e.g. `SI-0038C-2`) directly to its internal SAP Material
UUID, without requiring the material to have a Bill of Materials — this is
what lets us look up ANY material (including raw/purchased items that have
no BOM), not just manufactured/assembled products.

## What's failing right now
A live test call to this service, using the `_EMERGENTBOM` user, returns:

```
Authorization role missing for service QueryMaterialIn, operation
FindByElements
```

**Technical details of the call:**
- **SOAP Service:** `QueryMaterialIn`
- **Operation:** `MaterialByElementsQuery_sync` (SOAP action `FindByElements`)
- **Endpoint pattern:** `https://<tenant>.businessbydesign.cloud.sap/sap/bc/srt/scs/sap/querymaterialin`
- **Business user:** `_EMERGENTBOM`

## What we need done
1. Go to **Application and User Management → Communication Arrangements**.
2. Check whether a Communication Arrangement for the **"Query Materials"**
   scenario exists and is active. If not, create/activate it.
3. Go to **Application and User Management → Business Users**, open
   `_EMERGENTBOM`, and confirm its assigned business role includes access to
   the `QueryMaterialIn` service / `FindByElements` operation. Add this
   access if missing.
4. This should be a small addition to `_EMERGENTBOM`'s **existing** role —
   not a new user, new password, or new credential setup. QMS already
   proves this user can be authorized for material-related SAP services.

## How to confirm it's fixed
Once the role/scenario is added, let us know — we will immediately re-test
the exact same call from our side (no redeploy needed to verify) and
confirm the authorization error is gone.

## Why this matters
Once authorized, our app can:
- Resolve Standard Cost linkage for inventory items that currently show no
  match (raw/purchased materials with no BOM anywhere in the catalog).
- Offer a general "item lookup" in BOM Explorer that works even for
  materials with no BOM (currently, searching for such an item correctly
  shows "not found in SAP" because there's genuinely no BOM to explore, but
  we'd like to show its item details instead of just an error).
