"""SAP Business ByDesign Purchase Order query client (Supplier Portal
Phase 1, Aug 2026) - lets an approved external vendor see their own open
Purchase Orders inside the Supplier Portal.

Live-verified Aug 26 2026 against this tenant's real
`QueryPurchaseOrderQueryIn` service (SAP_SOAP_PO_ENDPOINT) using the same
`_EMERGENTBOM` technical user every other SOAP client in this app uses -
message `PurchaseOrderSimpleByElementsQuery_sync`. Two things confirmed
live that contradicted the public SAP help docs (which claim this
service is header-only):
  1. This tenant's response DOES include full `<PurchaseOrderItem>`
     line-item detail (ItemID, Quantity, Description, ProductKey/
     ProductID, DeliveryPeriod) directly - no second `ManagePurchaseOrderIn`
     Read call needed at all.
  2. The vendor's SAP Vendor Code (what the vendor types in at signup,
     `supplier_portal_accounts.vendor_code`) IS the same value as
     `PartySellerPartyKey/PartyID` - confirmed by filtering
     `SelectionBySellerPartyID` and getting only that seller's POs back.

SAP does not expose a per-item "already delivered" quantity on this
query (only header-level status codes) - `already_shipped_qty` /
`remaining_qty` are computed entirely from OUR OWN
`supplier_portal_shipments` history (see supplier_shipment_service.py),
same as before this endpoint existed. "Open" here just means
`DeliveryProcessingStatusCode` is not yet `3` (Finished) - SAP still
expects some delivery against this PO."""
import xml.etree.ElementTree as ET

import requests
from requests.auth import HTTPBasicAuth

from sap_rate_limiter import sap_semaphore

SOAP_ACTION = ""
FINISHED_DELIVERY_STATUS_CODE = "3"

_REQUEST_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">
<soapenv:Body>
<n1:PurchaseOrderSimpleByElementsQuery_sync xmlns:n1="http://sap.com/xi/SAPGlobal20/Global">
 <PurchaseOrderSimpleSelectionByElements>
  <SelectionBySellerPartyID>
   <InclusionExclusionCode>I</InclusionExclusionCode>
   <IntervalBoundaryTypeCode>1</IntervalBoundaryTypeCode>
   <LowerBoundarySellerPartyID>{vendor_code}</LowerBoundarySellerPartyID>
  </SelectionBySellerPartyID>
 </PurchaseOrderSimpleSelectionByElements>
 <ProcessingConditions>
  <QueryHitsMaximumNumberValue>{limit}</QueryHitsMaximumNumberValue>
  <QueryHitsUnlimitedIndicator>false</QueryHitsUnlimitedIndicator>
 </ProcessingConditions>
</n1:PurchaseOrderSimpleByElementsQuery_sync>
</soapenv:Body>
</soapenv:Envelope>"""


class SAPPurchaseOrderError(Exception):
    pass


class SAPPurchaseOrderNotConfiguredError(SAPPurchaseOrderError):
    """No SAP endpoint has been wired up yet - distinct from a live call
    that fails, so callers/UI can show "not connected yet" rather than a
    generic error."""
    pass


class SAPPurchaseOrderClient:
    FETCH_LIMIT = 200

    def __init__(self, endpoint: str, username: str, password: str):
        self.endpoint = endpoint or None
        self.auth = HTTPBasicAuth(username, password)

    def get_open_pos_for_vendor(self, vendor_code: str) -> list:
        if not self.endpoint:
            raise SAPPurchaseOrderNotConfiguredError(
                "SAP Purchase Order lookup isn't wired up yet - waiting on the SAP SOAP/OData "
                "endpoint (SAP_SOAP_PO_ENDPOINT) to be activated and shared for this tenant."
            )
        body = _REQUEST_TEMPLATE.format(vendor_code=vendor_code, limit=self.FETCH_LIMIT)
        try:
            with sap_semaphore:
                resp = requests.post(
                    self.endpoint, auth=self.auth, data=body.encode("utf-8"),
                    headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": SOAP_ACTION},
                    timeout=60,
                )
        except requests.exceptions.RequestException as e:
            raise SAPPurchaseOrderError(f"Could not reach SAP: {e}")

        if resp.status_code != 200 or "Fault" in resp.text[:2000]:
            raise SAPPurchaseOrderError(f"SAP Purchase Order query failed (HTTP {resp.status_code}): {resp.text[:400]}")

        root = ET.fromstring(resp.text)
        rows = []
        for po in root.findall(".//PurchaseOrder"):
            po_number = po.findtext("PurchaseOrderID")
            if po.findtext("DeliveryProcessingStatusCode") == FINISHED_DELIVERY_STATUS_CODE:
                continue
            for item in po.findall("PurchaseOrderItem"):
                qty_el = item.find("Quantity")
                due = item.findtext("DeliveryPeriod/EndDateTime")
                rows.append({
                    "po_number": po_number,
                    "item_number": item.findtext("ItemID"),
                    "product_id": item.findtext("ItemProduct/ProductKey/ProductID"),
                    "description": item.findtext("Description"),
                    "po_qty": float(qty_el.text) if qty_el is not None and qty_el.text else 0.0,
                    "unit_of_measure": qty_el.get("unitCode") if qty_el is not None else None,
                    "due_date": due.split("T")[0] if due else None,
                })
        return rows
