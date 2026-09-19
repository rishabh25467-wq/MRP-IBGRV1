# SAP Support Ticket Draft - Enable API-based Goods Receipt/Release for Stock Transfer Order Inbound Deliveries

Component: Inbound Logistics / Outbound Logistics / Site Logistics (custom OData
services `inboundstockemergent` and `odataoutboundemergent`; also tested the
standard-released BO `RequestOutboundDeliveryExecutionRun`)

## Summary
We need to fully automate Goods Issue and Goods Receipt for internal, intracompany
Stock Transfer Orders (STO) via API (OData/SOAP) - replacing a manual "click the
button in SAP UI" step for both the shipping side (Post Goods Issue / Release
Outbound Delivery) and the receiving side (Post Goods Receipt / Release Inbound
Delivery Notification). Every relevant action we've found and can call returns
"action is disabled", even on completely fresh, untouched, consistent documents.
We've tested this exhaustively across two related-but-distinct SAP KBAs and are
asking SAP to confirm whether this is a genuine Business Configuration/licensing
gap, and if so, how to enable it.

## Receiving side (this ticket's primary focus) - what we've tried and confirmed blocked

1. Custom OData Function Import `InboundDeliveryPGRBackground` on the
   `InboundDeliveryCollection` entity set (service `inboundstockemergent`) -
   calling this with a valid `ObjectID` returns HTTP 500 "action is disabled",
   confirmed live on a completely fresh, never-touched Inbound Delivery
   Notification (ruling out a stale-lock/already-processed artifact).
2. `InboundDeliveryRelease` (the prerequisite action, per SAP's own action
   model) is **also** disabled with the identical error, on the same fresh
   document - so this isn't specific to PGR, the entire release/receipt action
   chain is blocked.
3. We PATCHed the underlying `InboundDeliveryItemQuantityCollection` row to
   test adjusting the received quantity before calling PGRBackground - SAP
   rejects this too ("Changing data not possible; data is read-only").
4. We checked whether task-based execution (`QuerySiteLogisticsTaskIn` /
   `ManageSiteLogisticsTaskIn`) applies instead - zero matching Site Logistics
   Tasks for any real pending STO receipt (ProcessTypeCode=1 Inbound), so our
   tenant does not appear to route STO receiving through task-based execution
   either.
5. **New test (today)**: per SAP KBA **2691388** ("Action PGR_BACKGROUND not
   possible; action is disabled" for a custom solution on Stock Transfer
   Orders), we specifically re-tested against a receiving site (P8) that has a
   verified **active, Consistent, Released "Standard Receiving" Logistics
   Model with "Without Tasks" = Yes** (model `REC_P8`, in place since
   26-Apr-2023) - i.e. exactly the configuration KBA 2691388's scenario
   describes as the supported "direct PGR" path. We created a brand-new STO,
   posted Goods Issue on its Outbound Delivery, and called
   `InboundDeliveryRelease` then `InboundDeliveryPGRBackground` on the
   resulting fresh Inbound Delivery Notification before any UI interaction.
   Result: **identical "action is disabled" on both calls.** We also ran
   "Check Consistency" on the Inbound Delivery Notification in the UI - it
   reports fully Consistent, so this isn't a data-quality issue on our end.

## Shipping side - directly related finding from the same investigation

The exact same error class appears on the outbound/shipping side: the
standard-released Business Object `RequestOutboundDeliveryExecutionRun`
(UI: "Outbound Delivery Run", document type 833) exposes a released `Execute`
action via a Custom OData service we built and activated specifically to test
this (`deliveryrunem`). Calling `Execute` on our existing, already-Consistent
Outbound Delivery Run (`RDOCWT1`) returns the same error:
`"Action RequestOutboundDeliveryExecutionRunExecute not possible; action is
disabled"`. Separately, scheduling/activating that same Run in the UI itself
fails with `"MDRO instance is not active"`. We only mention this here because
it strongly suggests the SAME underlying tenant-level gap (task-based/Mass
Data Run-style logistics execution disabled) affects both directions of our
STO flow, not just the receiving side.

## What we're asking SAP Support

1. Please confirm whether KBA 3583076 and KBA 2691388 both still fully apply
   to our tenant as-is, given that our receiving site already has a Consistent,
   Released, "Without Tasks" Standard Receiving Logistics Model in place (the
   configuration KBA 2691388 describes as the supported direct-PGR scenario),
   yet the action is still disabled.
2. Is there a Business Configuration scoping option (mass data run /
   background job processing for Outbound Delivery Runs and/or Inbound
   Delivery Release-Receipt) that is currently NOT activated for our tenant,
   which would need to be turned on to unblock these actions? If so, please
   tell us exactly which scoping question/business option this is.
3. If this genuinely isn't possible today for either the Full-API PGR path
   (2691388) or task-based execution, is there any other supported
   OData/SOAP-based approach specifically for automating Goods Issue/Receipt
   on intracompany Stock Transfer Order deliveries (not vendor Purchase Order
   receiving, which works fine for us today via a different flow)?
4. Is this on any roadmap for future release, and in the meantime is there a
   recommended interim approach?

## Our tenant
- URL: my431827.businessbydesign.cloud.sap
- Relevant custom OData services: `inboundstockemergent`, `odataoutboundemergent`,
  `deliveryrunem` (test service created for this investigation, exposes
  `RequestOutboundDeliveryExecutionRunExecute`)
- Business scenario: internal multi-site Stock Transfer Orders (STO), NOT vendor
  purchase order receiving (which is a separate, working flow for us).
- Test evidence available on request: STO-000123 (Order 32514), Outbound
  Delivery P1D1-560 (GI posted, un-received), Inbound Delivery Notification
  P1D1-560 (Consistent, Release/PGR both disabled); Outbound Delivery Run
  RDOCWT1 (Consistent, `Execute` disabled).
