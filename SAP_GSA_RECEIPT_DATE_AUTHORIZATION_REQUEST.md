# SAP Authorization Request: Goods & Service Acknowledgement (Receipt Date)

## Who this is for
Your SAP Business ByDesign Basis/Admin team - same team that previously
granted access for `QuerySupplierIn`, `ManageProcurementPriceSpecificationIn`,
and `QuerySupplierInvoiceQueryIn`.

## Background
The "Purchase History from SAP" panel currently shows each Supplier
Invoice's own billing/**Invoice Date**. We want to add a second column,
**Receipt Date** - the date the physical goods actually arrived - next to
it, so buyers can see billing lag vs. real delivery timing at a glance.

## What's blocking this
There is no such field on the Supplier Invoice query we already use - it's
a genuinely separate SAP document (a Goods & Service Acknowledgement /
inbound delivery confirmation), not part of the invoice itself. We tried
guessing the standard endpoint URL pattern (following the same convention
as our other working integrations) and got empty, transport-level 500
responses for all of them - meaning no Communication Arrangement for this
scenario is active in this tenant yet at all (not even far enough along to
return a proper "missing authorization role" fault, which is what we saw
the first time for Supplier Invoice before that one got activated).

## What we need done
1. Go to **Application and User Management → Communication Arrangements →
   New Communication Arrangement**.
2. Search for a Communication Scenario named something like **"Goods and
   Service Acknowledgement Processing"**, **"Goods and Service
   Acknowledgement Integration"**, or **"Confirmation of Goods and Service
   Receipt"** (exact wording varies by SAP ByDesign release - if none of
   these show up, searching "Acknowledgement" or "Receipt" in the scenario
   picker should surface the right one).
3. Assign it to the same technical/business user already used for
   everything else in this app: **`_EMERGENTBOM`**.
4. Enable the query/read operation for that scenario (typically named
   `QueryGoodsAndServiceAcknowledgementIn` with operation `FindByElements`
   or `FindSimpleByElements` - whatever the arrangement wizard actually
   offers under that scenario) on `_EMERGENTBOM`'s business role.
5. Once created, the arrangement's **Technical Data** tab will show the
   generated SOAP endpoint URL for this tenant - please send that URL to
   us (it follows the same
   `https://my431827.businessbydesign.cloud.sap/sap/bc/srt/scs/sap/...`
   pattern as our other endpoints, but the exact path segment is only known
   once SAP generates it).
6. Let us know once done - we'll test the exact call from our side
   immediately (no redeploy needed) and confirm real receipt-date data
   comes back before adding it to the UI.

## Fallback if this scenario doesn't exist in your tenant
If your ByDesign edition/configuration doesn't expose this as a queryable
service at all, please let us know - we'll drop this column and keep
Invoice Date as the only date shown (which is already working correctly).
