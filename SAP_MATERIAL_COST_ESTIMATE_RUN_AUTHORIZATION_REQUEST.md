# SAP Authorization Request: Manage Material Cost Estimate Run

## Who this is for
Your SAP Business ByDesign Basis/Admin team - same team that previously
granted access for `QuerySupplierIn`, `ManageProcurementPriceSpecificationIn`,
`QuerySupplierInvoiceQueryIn`, etc.

## Background
BOM Explorer's "Total BOM Cost" is currently correct because we roll up
missing/zero-cost sub-assemblies from their own components on our side.
That's a good safety net, but the real fix is getting SAP itself to
calculate and save a genuine Standard Cost for those sub-assemblies (they
were simply never run through a Cost Estimate in SAP). We want to add a
"Run Cost Estimate" button in BOM Explorer next to any sub-assembly
showing a rolled-up (not-yet-costed) value, so a buyer/planner can trigger
SAP's own costing calculation for that specific item on demand.

## What we need done
1. Go to **Application and User Management → Communication Arrangements →
   New Communication Arrangement**.
2. Search for the Communication Scenario for **"Manage Material Cost
   Estimate Run"** (technical service name: `ManageMaterialCostEstimateRunDataBundle`,
   operation `MaintainBundle`) - listed under **Costing Processing** in the
   ByDesign web-service catalogue. If it doesn't show up by that exact
   name, try **User Management → Service Explorer** and search
   `ManageMaterialCostEstimateRunDataBundle` directly.
3. Assign it to the same technical/business user already used for
   everything else in this app: **`_EMERGENTBOM`**.
4. Give `_EMERGENTBOM` the minimum permission needed: submit + monitor
   Material Cost Estimate Runs (no need for broader Controlling/Costing
   admin access).
5. Once created, the arrangement's **Technical Data** tab will show the
   generated SOAP endpoint URL - please send that to us.

## Two extra values we need (see guidance below for how to find them)
This SOAP call requires a **Company ID** and a **Set of Books ID** - these
are one-time configuration values for your tenant, not something per-item.

### How to find your Company ID
- Go to the **General Ledger** or **Financials** work center → look for a
  "Companies" or "Company" master data view (sometimes under
  **Application and User Management → Organizational Management**).
- Your Company ID is usually a short code (e.g. `MC10000`-style) shown
  next to your company's legal name.
- Fastest shortcut: open any existing Purchase Order or Sales Order PDF -
  the Company ID is often printed in the document header/footer metadata,
  or visible if you check that document's "Company" field in its Business
  Data tab.

### How to find your Set of Books ID
- Go to **General Ledger** work center → **Master Data** → look for
  "Sets of Books" (sometimes called "Set of Books" or under Chart of
  Accounts configuration).
- If your company only has one, standard setups often use `0001` as the ID
  - but please confirm the actual value rather than assuming.

### Easiest fallback if the above is confusing
Go to the **Cost and Revenue** (or **Product Valuation**) work center →
**Periodic Tasks** → **Cost Run** / **New Cost Estimate Run**. That SAP UI
screen itself asks you to pick a Company and Set of Books from dropdowns
before it lets you create a run - whatever you'd normally select there IS
the value we need. You don't need to actually submit that screen; just
note which Company/Set of Books you'd pick.

## Once done
Send us: (1) the new SOAP endpoint URL, (2) your Company ID, (3) your Set
of Books ID. We'll wire up the "Run Cost Estimate" button, test it against
a single real under-costed item (e.g. `P26680`), and confirm the resulting
Standard Cost shows up correctly before rolling it out more broadly.
