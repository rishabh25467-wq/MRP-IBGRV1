# Plan: Remove Procurement, Supplier Management, and 4 Master Data items

## Objective
Turn this app into a dedicated Production/Inventory app by completely removing:
- The **Procurement** top navigation menu and everything under it (Procurement Planning,
  Create Purchase Order, Service Purchase Order, Created POs, Open Purchase Orders, Quota
  Allocation, Vendor Goods Receipt/GRN approval).
- The **Supplier Management** top navigation menu and everything under it (Supplier Master,
  Invite Supplier, Supplier Portal Approvals, Act as Supplier, and the shortcut into the
  Supplier Dashboard).
- The **external Supplier Portal** entirely (vendor login/signup, vendor shipment creation,
  vendor document upload, vendor audits/QC pages) - confirmed in scope, not just the internal
  menu shortcut to it.
- Four specific items inside the **Master Data** menu: **Component Master**, **Create
  Material**, **L1/L2 Item Report**, **Closing Inventory Report**.
- **Activate Material Site** (the one remaining Master Data item), **Production**, **Inventory
  Management** (Stock Overview, Inter Plant Stock Transfer, Inbound STO Receipt, Store Goods
  Issue), **BOM Management**, and **Administration** (SAP Write) all stay exactly as they are.

## What checking the actual code found
- **Vendor Goods Receipt (GRN approval)** - the page that lands a vendor-shipped Purchase
  Order's stock into warehouse inventory - currently lives inside the **Procurement** menu, not
  Supplier Management. It is powered by its own large, self-contained backend module built only
  for the vendor-shipment workflow. It reuses one shared utility file also used by the Inventory
  module's "Store Goods Issue" feature and by Inbound STO Receipt, but none of ITS OWN logic is
  used by anything staying in the app. Removing it does not break Inventory, STO, or Production.
- **Practical consequence of removing it**: once Vendor Goods Receipt and the Supplier Portal
  are gone, there is no in-app way left to receive a vendor-shipped Purchase Order's stock into
  inventory. This is fine if vendor deliveries are no longer being received this way and all
  inventory now moves through internal Stock Transfer Orders (STOs) instead - flagged here as
  the one thing to actively confirm, since it's the real functional change, not just a menu
  change.
- **Procurement Planning** (under Procurement) reads Minimum Stock Level values that are set on
  the Component Master page (one of the 4 Master Data items being removed). Since both are
  being removed together, this is not a loose end - nothing outside the removed set depends on
  it.
- No other cross-dependency was found running the other way: nothing in Production, Inventory,
  STO, BOM Management, or Store Approval reads from or calls into any of the pages/services
  being removed.

## Approach
1. Remove the Procurement and Supplier Management entries from the navigation, and the 4 named
   Master Data entries, leaving Activate Material Site as the only Master Data item.
2. Delete the frontend pages and routes exclusively serving what's removed: all Procurement
   pages, all Supplier Management pages, the entire external Supplier Portal (login, signup,
   dashboard, shipments, documents, audits), Component Master, Create Material, L1/L2 Item
   Report, and Closing Inventory Report.
3. Delete the backend endpoints and services that exclusively support those pages (including
   the vendor-shipment/GRN-approval service and any vendor-portal-only automation). The one
   shared utility file used for warehouse stock movement stays, since Store Goods Issue and
   Inbound STO Receipt still need it.
4. Existing database records tied to these modules (past purchase orders, vendor records,
   historical shipments, old master-data reports) are left in the database untouched - only the
   app's ability to view/create/edit them is removed. This is the safer, reversible choice; nothing
   requested a data wipe.
5. Confirm the app still starts and that Production, Inventory Management, BOM Management,
   Store Approval, and Administration all work exactly as before.

## Expected impact
- **Codebase weight**: a real, substantial drop - Procurement, Supplier Management, and the
  Supplier Portal each carry their own pages and backend logic (the vendor-shipment/GRN service
  alone is a large, self-contained module), all of which goes away outright.
- **App weight/speed**: fewer pages, routes, and backend endpoints to load and maintain; a
  noticeable but modest runtime speed-up, since this mainly reduces what has to be loaded rather
  than what's already slow today.
- **Risk to Production/Inventory**: low and specifically checked, not assumed - see findings
  above. The one open item is confirming vendor deliveries no longer need in-app receiving.

## Assumptions made (flag if any of these are wrong)
- Vendor-sourced procurement (Purchase Orders + receiving vendor shipments) is being phased out
  in favor of internal STOs only, so losing the in-app path to receive a vendor PO's stock is
  acceptable. If any vendor deliveries still need receiving into inventory after this change,
  say so before this is built - that would need a different, smaller-scope plan.
- Removing "code and menu access" is sufficient - no request has been made to delete or purge
  existing database records belonging to these modules.
