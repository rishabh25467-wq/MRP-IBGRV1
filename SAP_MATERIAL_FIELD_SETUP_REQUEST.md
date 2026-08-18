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

**Important: this needs Adaptation Mode, NOT Personalization Mode.**
Personalization (the flag/star/pin icons top-right of the Material
screen) only changes what YOU see - it can't link a field to a web
service. Adaptation Mode is a separate, admin-level mode:

1. On the Material screen, click your **user avatar icon** (top-right,
   the circular profile picture/icon - NOT the flag/star icons).
2. Select **Key User Settings** → **Start Adaptation Mode**. The screen
   title should now show "(Adaptation Mode)", not "(Personalization
   Mode)".
3. **Right-click** directly on the "Item Net Weight" field (or hover
   over it, a small icon may appear) → choose **Properties**.
4. In the Properties panel, go to the **Further Usage** tab → **Services**
   sub-tab. This lists every web service available on this screen.
5. Select **Query Material In** AND **Manage Material In**, then click
   **Add Field** for each.
6. Repeat steps 3-5 for the "Surface Area(Sq.Inch)" field.
7. **Save and Publish** the adaptation (top toolbar) - changes don't take
   effect until published - then exit Adaptation Mode.

(You do NOT need to do this for "Item Gross Weight", "Ray Item code", or
"Ray Item Description" - we only need the 2 fields above.)

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
