# Technical Request for SAP Basis/PDI Team — Expose "Inbound Delivery" (execution stage)

## Context
We already have a working custom OData service, `khinbounddelivery`, which
covers the **Inbound Delivery Notification** stage (`APDL_INBOUND_DELIVERY`).
For task-based warehouses (confirmed: site P1), SAP's own Document Flow
shows a further downstream chain after the Notification is released:

```
Inbound Delivery Notification  →  Warehouse Request  →  Warehouse Order  →  Inbound Delivery
      (khinbounddelivery)                                                   (NOT YET EXPOSED)
```

The final node — a genuinely different SAP Business Object from the
Notification — is where the real Goods Receipt / inventory posting
actually happens. We do not currently have any API (OData or SOAP) access
to it.

## Concrete reference example (from a real document)
Retrieved from the Notification's own printed form (`Print Notification`
action on `P8D1-231`, an STO inbound delivery at site P1):

```xml
<ConfirmedInboundDeliveryReference>
  <ID>54031</ID>
  <UUID>fa163e88-19fa-1fd1-add3-01fbacbd41b6</UUID>
  <TypeCode>24</TypeCode>
</ConfirmedInboundDeliveryReference>
```

- Document ID: `54031`
- UUID: `fa163e88-19fa-1fd1-add3-01fbacbd41b6`
- TypeCode: `24`
- Visible in SAP UI as the "Inbound Delivery" node in Document Flow
  (labelled just "Inbound Delivery", no "Notification" suffix), following
  Warehouse Request `86913` and Warehouse Order `102103`.

We confirmed this UUID does **not** exist in any of the 4 `kh*` custom
OData services already provided (`khinbounddelivery`,
`khinbounddeliveryrequest`, `khgoodsandactivityconfirmation`,
`khgoodsandserviceacknowledgement`) — checked via `UUID eq guid'...'`
filter against every EntitySet in all 4, zero matches everywhere.

## What we need exposed
A new (or extended) custom OData service covering this "Inbound Delivery"
(execution-stage) Business Object, with:

1. **Read access** to at least:
   - Header: `ID`, `ObjectID`, `UUID`, status fields (whatever this BO's
     equivalent of `ReleaseStatusCode`/`ProcessingStatusCode` is)
   - Item level: product, planned quantity, and — most importantly —
     the **actual/confirmed received quantity** (this is the field that
     was missing; the Notification-level `DeliveryQuantity` only ever
     shows the planned/shipped amount, never what was actually received)
2. **Write access** to whatever action finalizes/posts the Goods Receipt
   for this object (SAP's own UI presumably calls something like
   "Confirm"/"Post Goods Receipt" on this node — please expose the
   equivalent Function Import).

## Why this matters
Without this, our app can only reach the *first* stage (Notification) via
API. We can flip the Notification's own status to "Finished" via
`Release`, but this does **not** create a real inventory posting for
task-based sites — it just administratively closes the Notification and
hands off to the Warehouse Request/Order/Inbound-Delivery chain that we
can't see or act on. This is what happened in a live test we ran (with
the customer's explicit approval) on STO-000088 — the Notification shows
"Finished" but the real Goods Receipt / fulfilled quantity never posted.

Once this second-stage object is exposed, we can build reliable,
end-to-end automated STO receiving (single-line and multi-line orders)
without relying on manual SAP UI actions.
