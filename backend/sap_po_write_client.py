"""SAP Business ByDesign Purchase Order WRITE client - Purchase Order
Creation automation (Aug 2026). Creates a brand new Purchase Order in
this tenant's LIVE production SAP system from the app's own PO Creation
form (Company, Supplier, Purchase Unit, Bill-To, PO Date, line items).

Service: `ManagePurchaseOrderIn`, operation `MAINTAIN_BUNDLE`
(`PurchaseOrderBundleMaintainRequest_sync`), same envelope shape/
namespace (`http://sap.com/xi/A1S/Global`) as the sibling GSA write
client (sap_gsa_write_client.py). Endpoint already configured/reachable:
`SAP_SOAP_PO_MANAGE_ENDPOINT`.

Real field values CONFIRMED by reading a live PO (28792) via the
existing read-only sap_po_client.py (QueryPurchaseOrderQueryIn):
  - Company/Buyer party: PartyTypeCode 200, PartyID = "RI" or "RT"
    (this tenant's 2 legal entities - confirmed literal strings, not
    numeric SAP IDs).
  - Purchasing Unit: PartyTypeCode 200, PartyID = "{site}-PUR" (e.g.
    "P1-PUR").
  - Supplier/Seller: PartyTypeCode 147, PartyID = the supplier's
    `sap_internal_id` (confirmed identical to the Supplier Portal's own
    `vendor_code`, e.g. "H1330").
  - Product: ProductTypeCode 1, ProductIdentifierTypeCode 1, ProductID =
    the plain product_id string (same as inventory_cache/component_master).
  - Ship-to site: LocationID = the plain site code (e.g. "P1"), set at
    item level, no "-PUR" suffix.

UNVERIFIED, best-effort (no real historical PO exposed these on a READ
call - ByD's query schema differs from its write/maintain schema; per
SAP's own "Manage Purchase Orders" documentation) - will be corrected
from the very first real SAP fault message if wrong, since a create
either fully succeeds or fully fails (no partial/corrupt writes):
  - BillToParty: PartyTypeCode 200, PartyID = "RI"/"RT" (mirrors
    Company's shape - this tenant only has these 2 legal entities).
  - Header `<Date>` = the PO Date.
"""
import re

from xml.sax.saxutils import escape

import requests
from requests.auth import HTTPBasicAuth

from sap_rate_limiter import sap_semaphore

NAMESPACE = "http://sap.com/xi/A1S/Global"
SOAP_ACTION = ""


class SAPPurchaseOrderWriteError(Exception):
    pass


class SAPPurchaseOrderWriteNotConfiguredError(SAPPurchaseOrderWriteError):
    pass


def _extract_fault_message(raw_xml: str) -> str:
    """SAP's SOAP faults bury the one human-readable line inside
    <faultstring>...</faultstring> behind a wall of namespace
    boilerplate - surface just that."""
    match = re.search(r"<faultstring[^>]*>(.*?)</faultstring>", raw_xml, re.DOTALL)
    if match:
        return match.group(1).strip()
    return raw_xml[:400]


def _extract_log_errors(raw_xml: str) -> list:
    """A MAINTAIN_BUNDLE response can come back HTTP 200 with the create
    silently rejected, surfaced only inside the response's own <Log>
    section - best-effort extraction of the human-readable note text."""
    return re.findall(r"<Note[^>]*>([^<]+)</Note>", raw_xml)


_PARTY_TEMPLATE = """  <{tag} actionCode="01">
   <PartyKey>
    <PartyTypeCode>{party_type}</PartyTypeCode>
    <PartyID>{party_id}</PartyID>
   </PartyKey>
  </{tag}>
"""

_ITEM_TEMPLATE = """  <Item actionCode="01">
   <BusinessTransactionDocumentItemTypeCode>18</BusinessTransactionDocumentItemTypeCode>
   <Quantity unitCode="{unit_code}">{quantity}</Quantity>
   <NetUnitPrice>
    <Amount currencyCode="{currency}">{unit_price}</Amount>
    <BaseQuantity unitCode="{unit_code}">1</BaseQuantity>
   </NetUnitPrice>
   <DeliveryPeriod>
    <StartDateTime timeZoneCode="UTC">{delivery_date}T00:00:00Z</StartDateTime>
    <EndDateTime timeZoneCode="UTC">{delivery_date}T23:59:59Z</EndDateTime>
   </DeliveryPeriod>
   <ItemProduct>
    <ProductKey>
     <ProductTypeCode>1</ProductTypeCode>
     <ProductIdentifierTypeCode>1</ProductIdentifierTypeCode>
     <ProductID>{product_id}</ProductID>
    </ProductKey>
   </ItemProduct>
   <ShipToLocation actionCode="01">
    <LocationID>{site_id}</LocationID>
   </ShipToLocation>
  </Item>
"""

_ENVELOPE_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">
<soapenv:Body>
<n0:PurchaseOrderBundleMaintainRequest_sync xmlns:n0="{namespace}">
 <BasicMessageHeader/>
 <PurchaseOrderMaintainBundle actionCode="01">
  <BusinessTransactionDocumentTypeCode>001</BusinessTransactionDocumentTypeCode>
  <Date>{po_date}</Date>
  <CurrencyCode>{currency}</CurrencyCode>
{buyer_party}{purchasing_unit_party}{seller_party}{bill_to_party}{items}</PurchaseOrderMaintainBundle>
</n0:PurchaseOrderBundleMaintainRequest_sync>
</soapenv:Body>
</soapenv:Envelope>"""


class SAPPurchaseOrderWriteClient:
    def __init__(self, endpoint: str, username: str, password: str, timeout: int = 60):
        self.endpoint = endpoint or None
        self.auth = HTTPBasicAuth(username, password)
        self.timeout = timeout

    def create_purchase_order(
        self, company_code: str, purchase_unit_site: str, supplier_code: str,
        bill_to_company_code: str, po_date: str, currency: str, items: list,
    ) -> dict:
        """items: [{"product_id", "quantity", "unit_of_measure",
        "unit_price", "delivery_date" (YYYY-MM-DD), "site_id"}, ...].
        Returns {"po_number": str|None, "po_uuid": str|None, "raw_xml": str}.
        Raises SAPPurchaseOrderWriteError on any rejection (transport,
        HTTP fault, or a Log-reported business error)."""
        if not self.endpoint:
            raise SAPPurchaseOrderWriteNotConfiguredError(
                "SAP Purchase Order creation isn't wired up yet - SAP_SOAP_PO_MANAGE_ENDPOINT is not set."
            )
        buyer_party = _PARTY_TEMPLATE.format(tag="BuyerParty", party_type="200", party_id=escape(company_code))
        purchasing_unit_party = _PARTY_TEMPLATE.format(
            tag="PartyResponsiblePurchasingUnitParty", party_type="200",
            party_id=escape(f"{purchase_unit_site}-PUR"),
        )
        seller_party = _PARTY_TEMPLATE.format(tag="SellerParty", party_type="147", party_id=escape(supplier_code))
        bill_to_party = _PARTY_TEMPLATE.format(tag="BillToParty", party_type="200", party_id=escape(bill_to_company_code))
        items_xml = "".join(
            _ITEM_TEMPLATE.format(
                unit_code=escape(it["unit_of_measure"] or "EA"), quantity=it["quantity"],
                currency=escape(currency), unit_price=it["unit_price"],
                delivery_date=it["delivery_date"], product_id=escape(str(it["product_id"])),
                site_id=escape(str(it["site_id"])),
            ) for it in items
        )
        envelope = _ENVELOPE_TEMPLATE.format(
            namespace=NAMESPACE, po_date=po_date, currency=escape(currency),
            buyer_party=buyer_party, purchasing_unit_party=purchasing_unit_party,
            seller_party=seller_party, bill_to_party=bill_to_party, items=items_xml,
        )
        headers = {"Content-Type": "text/xml; charset=utf-8", "SOAPAction": SOAP_ACTION}
        try:
            with sap_semaphore:
                resp = requests.post(
                    self.endpoint, data=envelope.encode("utf-8"), headers=headers,
                    auth=self.auth, timeout=self.timeout,
                )
        except requests.exceptions.RequestException as e:
            raise SAPPurchaseOrderWriteError(f"SAP Purchase Order service unreachable: {e}")
        if resp.status_code != 200:
            raise SAPPurchaseOrderWriteError(
                f"SAP rejected the Purchase Order (HTTP {resp.status_code}): {_extract_fault_message(resp.text)}"
            )
        po_id_match = re.search(r"<PurchaseOrderID>([^<]+)</PurchaseOrderID>", resp.text)
        po_uuid_match = re.search(r"<PurchaseOrderUUID>([^<]+)</PurchaseOrderUUID>", resp.text)
        if not po_id_match:
            errors = _extract_log_errors(resp.text)
            raise SAPPurchaseOrderWriteError(
                "SAP did not return a Purchase Order number: " + ("; ".join(errors) if errors else resp.text[:400])
            )
        return {
            "po_number": po_id_match.group(1),
            "po_uuid": po_uuid_match.group(1) if po_uuid_match else None,
            "raw_xml": resp.text[:3000],
        }
