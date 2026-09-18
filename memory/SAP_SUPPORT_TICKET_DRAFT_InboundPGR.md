# SAP Support Ticket Draft - Enable Goods Receipt via API for Inbound Deliveries

Component: Inbound Logistics / Site Logistics (custom OData service `inboundstockemergent`)

## Summary
We need to post Goods Receipt (the equivalent of the UI's "Post Goods Receipt As
Planned" button on an Inbound Delivery Notification) via API, to replace a manual
process for our internal Stock Transfer Order receiving. Today this can only be
done by a human clicking the button in the SAP Fiori/HTML5 UI.

## What we've tried and confirmed blocked
1. Custom OData Function Import `InboundDeliveryPGRBackground` on the
   `InboundDeliveryCollection` entity set - calling this with a valid `ObjectID`
   returns HTTP 500 "action is disabled", confirmed live on a completely fresh,
   never-touched Inbound Delivery Notification (ruling out a stale-lock/already-
   processed artifact).
2. This matches SAP's own published KBA **3583076** ("Inability to Post Goods
   Receipt with Actual Quantities via OData API in Inbound Delivery Processing")
   - which we understand explains that Actual Quantity belongs to the "Confirmed
   Inbound Delivery" object, not the Notification, and that there is currently no
   web service/API to create a Confirmed Inbound Delivery directly.
3. We also tried PATCHing the underlying `InboundDeliveryItemQuantityCollection`
   row to adjust the received quantity before calling PGRBackground - SAP rejects
   this too ("Changing data not possible; data is read-only").
4. We checked the Site Logistics Task-based path (`QuerySiteLogisticsTaskIn` /
   `ManageSiteLogisticsTaskIn`) as an alternative - our tenant's inbound STO
   deliveries do not go through task-based execution at all (confirmed via a
   direct query - zero matching tasks for real pending STO receipts), so this
   path does not apply to our actual receiving process either.

## What we're asking SAP Support
1. Please confirm whether KBA 3583076 still fully applies to our tenant, or
   whether there is now a supported way (OData, SOAP, or otherwise) to post a
   Goods Receipt with the correct Actual Quantity against an EXISTING Inbound
   Delivery Notification (i.e., the equivalent of the UI's "Post Goods Receipt
   As Planned" action) - for deliveries generated automatically by a Stock
   Transfer Order (not tied to a Purchase Order).
2. If no such API exists today, is there a supported configuration change
   (e.g. a different Logistics Model / warehouse settings) that would let us
   post Goods Receipt via API for our specific use case?
3. If this genuinely isn't possible today, is it on any SAP roadmap, and is
   there a recommended alternative integration approach for automating Goods
   Receipt on Stock Transfer Order deliveries specifically?

## Our tenant
- URL: my431827.businessbydesign.cloud.sap
- Relevant custom OData service: `inboundstockemergent`
- Business scenario: internal multi-site Stock Transfer Orders (STO), NOT vendor
  purchase order receiving (which is a separate, working flow for us).
