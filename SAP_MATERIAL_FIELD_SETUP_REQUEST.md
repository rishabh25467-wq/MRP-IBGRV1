# SAP Setup Request: Net Weight & Surface Area sync (Admin page <-> SAP)

## Who this is for
Your SAP Business ByDesign Basis/Admin team (whoever manages Key User Tools
custom fields and Communication Arrangements).

## Background
This app's Admin page has a "Weight/Area" feature to enter a component's
**Net Weight** and **Surface Area**, and push those values into SAP. We
can see you've already created these as custom fields directly on the
Material's **General** tab (screenshot confirms Material `5989825-2.1`
has "Surface Area(Sq.Inch)" = 255) - great start. Two more steps are
needed before our app can read/write them:

## Step 1: Link the 2 custom fields to the web services
A custom field only appears in a SOAP web service response once it's
explicitly linked to that service. Live-confirmed (18 Aug 2026): querying
Material `5989825-2.1` via `QueryMaterialIn` today returns NEITHER "Item
Net Weight" nor "Surface Area(Sq.Inch)" at all, even though Surface Area
has a real value (255) in the SAP UI.

**To fix, for each of the 2 fields ("Item Net Weight" and "Surface
Area(Sq.Inch)" - you do NOT need to do this for "Item Gross Weight", "Ray
Item code", or "Ray Item Description", we don't need those):**
1. Open the Material screen, enter **Adaptation Mode** (the pencil/edit
   icon usually top-right).
2. Click the field ("Item Net Weight" or "Surface Area(Sq.Inch)") to open
   its properties, then go to **Further Usage → Services**.
3. This lists every web service available on that screen - select **Query
   Material In** AND **Manage Material In**, then click **Add Field** for
   each.
4. Repeat for the other field.

## Step 2: Activate the "Manage Materials" write service
Separately, writing anything back to SAP (not just these 2 fields) needs
its own Communication Arrangement, which doesn't exist on this tenant yet
at all (a probe call today returns a generic "Web service processing
error" fault). Please also:
1. Go to **Application and User Management → Communication Arrangements**.
2. Create a new arrangement using scenario **"Manage Materials"** (same
   self-service flow you've used before for "Query Materials",
   "Production BOM Query", etc.) - linked to the same technical user
   `_EMERGENTBOM`.

## How to confirm it's fixed
Once both steps are done, just let us know - we'll re-query Material
`5989825-2.1` live from our side (no redeploy needed) to confirm both
fields now show up, and update our config with the exact field names SAP
assigns.

## Why this matters
Once working, anyone can enter Net Weight/Surface Area for a component
directly in this app's Admin page and push it straight into SAP - no need
to open the Material screen in SAP UI for routine data entry.
