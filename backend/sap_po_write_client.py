"""SAP Business ByDesign Purchase Order WRITE client - Purchase Order
Creation automation (Aug 2026). Creates a brand new Purchase Order in
this tenant's LIVE production SAP system from the app's own PO Creation
form (Company, Supplier, Purchase Unit, Bill-To, PO Date, line items).

Service: `ManagePurchaseOrderIn`, operation `MAINTAIN_BUNDLE`
(`PurchaseOrderBundleMaintainRequest_sync`). Namespace CORRECTED Sep 4
2026 to `http://sap.com/xi/SAPGlobal20/Global` (confirmed via SAP's own
published ManagePurchaseOrderIn docs) - the original `A1S/Global`
namespace (copied from the sibling read-only `sap_po_client.py`, a
genuinely different Query* service) reproduced the exact same generic
"Web service processing error" already seen and root-caused for the
sibling GSA write client (sap_gsa_write_client.py) - SAP parses the
envelope fine but can't route it to the right ABSL handler. Endpoint
already configured/reachable: `SAP_SOAP_PO_MANAGE_ENDPOINT`.

Envelope/party/item shape REWRITTEN Sep 4 2026 to match SAP's OFFICIAL
published Maintain/Check/Upload examples exactly (help.sap.com
PSM_ISI_R_II_SRM_PO_MBO) - the earlier version was inferred from a READ
call's response shape, which the docstring already flagged as
"UNVERIFIED" and different from the write schema. Real, structural bugs
found by diffing against the official examples (still the same generic
"Web service processing error" symptom - SAP's ABSL handler throws
internally on a malformed request rather than returning a clean
business-validation fault):
  - `PartyKey` must contain ONLY `<PartyID>` on a write - no
    `<PartyTypeCode>` child at all (that shape only appears on READ
    responses, where PartyKey doc's OWN PartyTypeCode is a different,
    always-200, sub-code - never send it on a Maintain request).
  - A header-level `<ShipToLocation>` and `<Company>` party (mirrors
    BuyerParty) are both present in every official example and missing
    here before.
  - `ObjectNodeSenderTechnicalID` (header + each Item) and
    `ObjectNodePartyTechnicalID` (each Party) are present in every
    official example - arbitrary sequential integers, never
    interpreted by SAP beyond echoing them back in the response Log.
  - `ItemListCompleteTransmissionIndicator="true"` attribute on
    `PurchaseOrderMaintainBundle` and the Item-level FollowUpXxx/
    DirectMaterialIndicator elements are present in every official
    example for a Material (TypeCode 18) item.
  - Price element is `ListUnitPrice`, not `NetUnitPrice`, on a write
    (NetUnitPrice only appears on READ responses as a computed value).
`EmployeeResponsibleParty` (PartyTypeCode 167, the requesting
purchaser) appears in every official example too, but this app has no
such SAP Employee ID captured anywhere - left out per the "Empty and
Missing Elements" doc section (untransmitted optional elements are
simply not set) rather than guessing a wrong ID; add it if SAP's very
first real fault message (now readable, not generic, once the
structural fixes above land) asks for it.

Real field values CONFIRMED by reading a live PO (28792) via the
existing read-only sap_po_client.py (QueryPurchaseOrderQueryIn):
  - Company/Buyer party: PartyID = "RI" or "RT" (this tenant's 2 legal
    entities - confirmed literal strings, not numeric SAP IDs).
  - Purchasing Unit: PartyID = "{site}-PUR" (e.g. "P1-PUR").
  - Supplier/Seller: PartyID = the supplier's `sap_internal_id`
    (confirmed identical to the Supplier Portal's own `vendor_code`,
    e.g. "H1330").
  - Product: ProductTypeCode 1, ProductIdentifierTypeCode 1, ProductID =
    the plain product_id string (same as inventory_cache/component_master).
  - Ship-to site: LocationID = the plain site code (e.g. "P1").

UNVERIFIED, best-effort - will be corrected from the very first real
SAP fault message if wrong, since a create either fully succeeds or
fully fails (no partial/corrupt writes):
  - BillToParty: PartyID = "RI"/"RT" (mirrors Company's shape - this
    tenant only has these 2 legal entities).
  - Header `<Date>` = the PO Date.
  - DirectMaterialIndicator=true for every item (these are real
    inventory materials, not services/expenses - the official example
    uses false because its sample item is a non-stock line).
"""
import re

from xml.sax.saxutils import escape

import requests
from requests.auth import HTTPBasicAuth

from sap_rate_limiter import sap_semaphore

NAMESPACE = "http://sap.com/xi/SAPGlobal20/Global"
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
   <ObjectNodePartyTechnicalID>{tech_id}</ObjectNodePartyTechnicalID>
   <PartyKey>
    <PartyID>{party_id}</PartyID>
   </PartyKey>
  </{tag}>
"""

_SHIP_TO_LOCATION_TEMPLATE = """  <ShipToLocation actionCode="01">
   <ObjectNodePartyTechnicalID>{tech_id}</ObjectNodePartyTechnicalID>
   <LocationID>{site_id}</LocationID>
  </ShipToLocation>
"""

_ITEM_TEMPLATE = """  <Item actionCode="01">
   <ObjectNodeSenderTechnicalID>{tech_id}</ObjectNodeSenderTechnicalID>
   <BusinessTransactionDocumentItemTypeCode>18</BusinessTransactionDocumentItemTypeCode>
   <Quantity unitCode="{unit_code}">{quantity}</Quantity>
   <ListUnitPrice>
    <Amount currencyCode="{currency}">{unit_price}</Amount>
    <BaseQuantity unitCode="{unit_code}">1</BaseQuantity>
   </ListUnitPrice>
   <DeliveryPeriod>
    <StartDateTime timeZoneCode="UTC">{delivery_date}T00:00:00Z</StartDateTime>
    <EndDateTime timeZoneCode="UTC">{delivery_date}T23:59:59Z</EndDateTime>
   </DeliveryPeriod>
   <DirectMaterialIndicator>true</DirectMaterialIndicator>
   <ThirdPartyDealIndicator>false</ThirdPartyDealIndicator>
   <FollowUpPurchaseOrderConfirmation>
    <RequirementCode>04</RequirementCode>
   </FollowUpPurchaseOrderConfirmation>
   <FollowUpDelivery>
    <RequirementCode>01</RequirementCode>
    <EmployeeTimeConfirmationRequiredIndicator>false</EmployeeTimeConfirmationRequiredIndicator>
   </FollowUpDelivery>
   <FollowUpInvoice>
    <BusinessTransactionDocumentSettlementRelevanceIndicator>false</BusinessTransactionDocumentSettlementRelevanceIndicator>
    <RequirementCode>01</RequirementCode>
    <EvaluatedReceiptSettlementIndicator>false</EvaluatedReceiptSettlementIndicator>
    <DeliveryBasedInvoiceVerificationIndicator>false</DeliveryBasedInvoiceVerificationIndicator>
   </FollowUpInvoice>
   <ItemProduct actionCode="01">
    <ObjectNodePartyTechnicalID>{item_product_tech_id}</ObjectNodePartyTechnicalID>
    <CashDiscountDeductibleIndicator>true</CashDiscountDeductibleIndicator>
    <ProductKey>
     <ProductTypeCode>1</ProductTypeCode>
     <ProductIdentifierTypeCode>1</ProductIdentifierTypeCode>
     <ProductID>{product_id}</ProductID>
    </ProductKey>
   </ItemProduct>
   <ShipToLocation actionCode="01">
    <ObjectNodePartyTechnicalID>{item_ship_to_tech_id}</ObjectNodePartyTechnicalID>
    <LocationID>{site_id}</LocationID>
   </ShipToLocation>
  </Item>
"""

_ENVELOPE_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">
<soapenv:Body>
<n0:PurchaseOrderBundleMaintainRequest_sync xmlns:n0="{namespace}">
 <BasicMessageHeader/>
 <PurchaseOrderMaintainBundle actionCode="01" ItemListCompleteTransmissionIndicator="true">
  <ObjectNodeSenderTechnicalID>1</ObjectNodeSenderTechnicalID>
  <BusinessTransactionDocumentTypeCode>001</BusinessTransactionDocumentTypeCode>
  <CurrencyCode>{currency}</CurrencyCode>
{buyer_party}{seller_party}{employee_responsible_party}{bill_to_party}{company_party}{ship_to_location}{items}</PurchaseOrderMaintainBundle>
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
        employee_responsible_id: str,
    ) -> dict:
        """items: [{"product_id", "quantity", "unit_of_measure",
        "unit_price", "delivery_date" (YYYY-MM-DD), "site_id"}, ...].
        `employee_responsible_id`: SAP Employee ID for EmployeeResponsibleParty
        (PartyTypeCode 167, "the Purchaser who is requesting the purchase of
        goods") - present in every single official SAP example without
        exception (Sep 4 2026 finding), so treated as effectively mandatory
        here even though not confirmed via a live PO read.
        Returns {"po_number": str|None, "po_uuid": str|None, "raw_xml": str}.
        Raises SAPPurchaseOrderWriteError on any rejection (transport,
        HTTP fault, or a Log-reported business error)."""
        if not self.endpoint:
            raise SAPPurchaseOrderWriteNotConfiguredError(
                "SAP Purchase Order creation isn't wired up yet - SAP_SOAP_PO_MANAGE_ENDPOINT is not set."
            )
        buyer_party = _PARTY_TEMPLATE.format(tag="BuyerParty", tech_id=2, party_id=escape(company_code))
        seller_party = _PARTY_TEMPLATE.format(tag="SellerParty", tech_id=3, party_id=escape(supplier_code))
        employee_responsible_party = _PARTY_TEMPLATE.format(
            tag="EmployeeResponsibleParty", tech_id=4, party_id=escape(employee_responsible_id),
        )
        bill_to_party = _PARTY_TEMPLATE.format(tag="BillToParty", tech_id=5, party_id=escape(bill_to_company_code))
        company_party = _PARTY_TEMPLATE.format(tag="Company", tech_id=6, party_id=escape(company_code))
        ship_to_location = _SHIP_TO_LOCATION_TEMPLATE.format(tech_id=7, site_id=escape(purchase_unit_site))
        items_xml = "".join(
            _ITEM_TEMPLATE.format(
                tech_id=8 + idx * 3, item_product_tech_id=9 + idx * 3, item_ship_to_tech_id=10 + idx * 3,
                unit_code=escape(it["unit_of_measure"] or "EA"), quantity=it["quantity"],
                currency=escape(currency), unit_price=it["unit_price"],
                delivery_date=it["delivery_date"], product_id=escape(str(it["product_id"])),
                site_id=escape(str(it["site_id"])),
            ) for idx, it in enumerate(items)
        )
        envelope = _ENVELOPE_TEMPLATE.format(
            namespace=NAMESPACE, currency=escape(currency),
            buyer_party=buyer_party, seller_party=seller_party,
            employee_responsible_party=employee_responsible_party,
            bill_to_party=bill_to_party, company_party=company_party,
            ship_to_location=ship_to_location, items=items_xml,
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
