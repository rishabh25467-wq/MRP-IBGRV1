# SAP Authorization Request: Supplier Invoice History Read (QuerySupplierInvoiceQueryIn)

## Who this is for
Your SAP Business ByDesign Basis/Admin team (or whoever manages Communication
Arrangements and Business User roles in your SAP tenant) - same team that
previously granted access for `QuerySupplierIn` and `ManageProcurementPriceSpecificationIn`.

## Background
To show real Purchase Order/Supplier Invoice history (supplier name + price +
quantity + date) straight from SAP itself - alongside the existing external
ERP price history and SAP Price Specification panels - the app needs to call
the standard SAP service **`QuerySupplierInvoiceQueryIn`**, using the same
technical user already used for BOM/Material/Supplier/Price Spec calls,
**`_EMERGENTBOM`**.

## What's failing right now
A live test call to the endpoint below returned a SPECIFIC, actionable fault
(not a generic error) - this means the service endpoint itself already
exists and is reachable, but the technical user is missing the authorization
role for it:

```
Authorization role missing for service "ServiceInterface http://sap.com/xi/A1S/Global
QuerySupplierInvoiceQueryIn <default> <default>", operation "Operation
http://sap.com/xi/A1S/Global FindSimpleByElements"
```

**Technical details of the call:**
- **SOAP Service:** `QuerySupplierInvoiceQueryIn`
- **Operation:** `FindSimpleByElements` (SOAP action `QUERY_BY_ELEMENTS`)
- **Endpoint used:** `https://my431827.businessbydesign.cloud.sap/sap/bc/srt/scs/sap/querysupplierinvoicequeryin?sap-vhost=my431827.businessbydesign.cloud.sap`
- **Business/technical user:** `_EMERGENTBOM`

## What we need done
1. Go to **Application and User Management → Communication Arrangements**
   (or **Business Users → `_EMERGENTBOM` → Edit Access Rights**).
2. Find the Communication Arrangement/Scenario tied to `_EMERGENTBOM` that
   already covers Supplier Invoicing / Procurement queries (or create one if
   none exists for "Supplier Invoice Processing").
3. Add/enable the **`QuerySupplierInvoiceQueryIn`** service interface
   (operation `FindSimpleByElements`) to that arrangement's business role.
4. (Optional, nice-to-have) If you also want real Purchase Order history
   (not just invoiced/billed history), also enable **`QueryPurchaseOrderQueryIn`**
   the same way - our test call to that one returned a generic "Web service
   processing error" rather than the specific auth fault above, so it's
   unclear yet whether it needs the same role, a different one, or isn't
   activated at all in this tenant. We'll re-test once Supplier Invoice
   access is confirmed working.
5. Once active, let us know - we'll re-test the exact same call from our
   side immediately (no redeploy needed) and confirm data comes back.

## Why this matters
Once working, a new "Purchase History from SAP" panel (next to the existing
ERP and SAP Price Spec panels on the Suppliers page) will show real,
SAP-native Supplier Invoice records per Product ID - supplier name, net unit
price, quantity, and date - as an authoritative cross-check against the
external ERP price history and a way to spot items that are actively
purchased but have no formal SAP Price Specification on file yet (like
`SCR410WM`, discussed earlier).
