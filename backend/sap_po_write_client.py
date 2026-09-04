"""SAP Business ByDesign Purchase Order WRITE client - Purchase Order
Creation automation (Aug 2026). Creates a brand new Purchase Order in
this tenant's LIVE production SAP system from the app's own PO Creation
form (Company, Supplier, Purchase Unit, Bill-To, PO Date, line items).

CONFIRMED WORKING LIVE Sep 4 2026 (real PO 29173 created, UUID
fa163e47-9469-1fe1-aa84-e93dc20ed1f9, empty Log = zero errors/warnings)
after 3 real, confirmed root causes were found and fixed - all 3
produced the exact same generic, useless SAP fault
("Web service processing error; more details in the web service error
log on provider side") with no indication of which one was wrong, so
each had to be isolated one at a time against the LIVE tenant (a Check/
simulate call reproduces the identical failure, ruling out anything
data-specific and confirming it's a payload/schema-shape bug, not an
authorization or Communication Arrangement gap - both were verified
separately: the Communication Arrangement shows all 4 operations
Released, and even the full-admin ItAdmin credentials hit the same
error the technical user did):

1. Namespace: `http://sap.com/xi/A1S/Global` (copied from the sibling
   read-only `sap_po_client.py`, a genuinely different Query* service)
   -> corrected to `http://sap.com/xi/SAPGlobal20/Global` (confirmed via
   SAP's own published ManagePurchaseOrderIn docs at help.sap.com,
   PSM_ISI_R_II_SRM_PO_MBO). Same exact mistake already found once for
   the sibling GSA write client (sap_gsa_write_client.py).
2. Envelope/party/item shape didn't match SAP's OFFICIAL published
   Maintain/Check/Upload examples (the original version was inferred
   from a READ call's response shape, which is a different schema):
     - `PartyKey` must contain ONLY `<PartyID>` on a write - no
       `<PartyTypeCode>` child (that only appears on READ responses).
     - Header-level `<Company>` party (mirrors BuyerParty) and
       `<ShipToLocation>` are both required.
     - `ObjectNodeSenderTechnicalID` (header + each Item) and
       `ObjectNodePartyTechnicalID` (each Party/ItemProduct/item
       ShipToLocation) are required - arbitrary sequential integers,
       only ever echoed back, never interpreted by SAP.
     - `ItemListCompleteTransmissionIndicator="true"` attribute, plus
       Item-level `DirectMaterialIndicator`/`ThirdPartyDealIndicator`/
       `FollowUpPurchaseOrderConfirmation`/`FollowUpDelivery`/
       `FollowUpInvoice` (each with their own sub-indicators) are all
       required for a Material (TypeCode 18) item.
     - Price element is `ListUnitPrice`, not `NetUnitPrice` (NetUnitPrice
       only appears on READ responses as a computed value).
3. `EmployeeResponsibleParty` (PartyTypeCode 167, "the Purchaser who is
   requesting the purchase of goods") was missing entirely - present in
   every single official example with no exception, but this app never
   captured a per-user SAP Employee ID anywhere. Confirmed as the last
   real blocker: adding it with an obviously-wrong PartyID ("RT", a
   company code) immediately turned the generic crash into a real,
   readable business fault ("Employee responsible missing; Buyer
   Responsible RT is not valid") - proof the element itself was the
   fix, only the value was wrong. The user then confirmed live in SAP's
   own "New Purchase Order" UI that logging in as `ItAdmin` auto-fills
   "Buyer Responsible: 1 - Admin Ramp" - PartyID `"1"` is that same
   confirmed-real Employee ID, now hardcoded in server.py as
   `PO_EMPLOYEE_RESPONSIBLE_ID` (fixed system responsible party for
   every PO created via Emergent - no per-creator SAP Employee mapping
   exists in this app).

`Purchasing Unit` (`PurchasingUnitParty`, PartyTypeCode 410) IS
confirmed as a real official header party by SAP's own docs (an
earlier note here wrongly said it was removed/not in any example) -
still not sent explicitly though, since it reliably auto-derives once
Business Residence is set correctly (confirmed on PO 29176) and
guessing its exact schema sequence position wrong risks a full SOAP
fault, unlike a trailing extension field.

Response parsing: a real success uses `<BusinessTransactionDocumentID>`
+ `<UUID>` (confirmed live, PO 29173) - NOT `<PurchaseOrderID>`/
`<PurchaseOrderUUID>` (that was a guess based on the sibling read
client's differently-named fields).

Real field values CONFIRMED by reading a live PO (28792) via the
existing read-only sap_po_client.py (QueryPurchaseOrderQueryIn):
  - Company/Buyer party: PartyID = "RI" or "RT" (this tenant's 2 legal
    entities - confirmed literal strings, not numeric SAP IDs).
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
  - DirectMaterialIndicator=true for every item (these are real
    inventory materials, not services/expenses - the official example
    uses false because its sample item is a non-stock line).

SEP 5 2026 REAL FIX (this session, superseding the note above): the
"one-off" namespace fields turned out to be a dead end for writing -
confirmed by creating a real live test PO (29181) with them set and
reading it back still blank, no error at all. Root cause (found by the
user directly in SAP's own Adaptation Mode "Services" tab, per field):
this tenant has a SEPARATE, NEWER set of Key User extension fields
(namespace `http://sap.com/xi/AP/CustomerExtension/BYD/A4CF6`, distinct
from the old "one-off" one) that ARE explicitly enabled ("Field
Available" checked) for the exact write message this client sends
(`PurchaseOrderBundleMaintainRequest_sync`, service `ManagePurchaseOrderIn`,
operation `ManagePurchaseOrderInMaintainB...`):
  - PO Date -> field name `PODate` (confirmed via screenshot of the
    field's own "Services" tab).
  - Portal PR Number -> field name `PortalPRNumber` (same, confirmed).
Both use `CUSTOM_FIELD_NAMESPACE` below. Business Residence was
confirmed to NOT be a custom/key-user field at all (real standard SAP
field, value-help shows site codes like "P1"/"P2" with names like "RAY
INTERNATIONAL-P1") - its exact technical element name could not be
found via SAP UI (Ctrl+F1 Technical Help didn't surface it), so
`BusinessResidenceID` (plain header element, no namespace prefix,
following the same naming convention SAP uses for this concept on
sibling Manage*In services like ManageMaterialIn) is used as a
best-effort guess, placed at the very end of the bundle (safe position
- if wrong, silently ignored like the old one-off attempt, doesn't
break the rest of the create). `Date` and `PurchasingUnitParty`
(PartyTypeCode 410) are BOTH confirmed as real standard fields by SAP's
own official ManagePurchaseOrderIn documentation (help.sap.com,
PSM_ISI_R_II_SRM_PO_MBO) - `Date` is sent as a plain header element
right after `CurrencyCode` (matches the docs' listed field order).
`PurchasingUnitParty` is NOT sent explicitly (still relying on it
auto-deriving once Business Residence lands correctly, per the PO
29176 evidence) since guessing its exact schema position wrong risks
breaking the entire create with a real SOAP fault, unlike a trailing
extension field which just gets silently ignored if wrong.
"""
import re

from xml.sax.saxutils import escape

import requests
from requests.auth import HTTPBasicAuth

from sap_rate_limiter import sap_semaphore

NAMESPACE = "http://sap.com/xi/SAPGlobal20/Global"
SOAP_ACTION = ""

# Sep 5 2026 CONFIRMED fix - see module docstring. Verified live via the
# SAP UI's own "Further Usage of Extension Field" > "Services" tab for
# both fields, both checked ("Field Available") for exactly the message
# this client sends (PurchaseOrderBundleMaintainRequest_sync).
CUSTOM_FIELD_NAMESPACE = "http://sap.com/xi/AP/CustomerExtension/BYD/A4CF6"

# Business Residence: confirmed NOT a Key User extension field (real
# standard SAP field per the user's own Adaptation Mode search), but its
# real technical element name couldn't be found via SAP UI (Ctrl+F1
# Technical Help didn't surface it either). Sep 5 2026 best-effort:
# reuse this OLDER one-off namespace's `BusinesResidence` element
# (single "s", confirmed real live values like "P1" via a read-only
# query on several real POs) but placed in the SAME header position
# (right after `<Date>`) that turned out to be required for `PODate`/
# `PortalPRNumber` to actually persist - the original Sep 4 attempt at
# this exact field failed only because it was trailing after Item, the
# same positional mistake made with PODate initially.
OLD_CUSTOM_FIELD_NAMESPACE = "http://0012819041-one-off.sap.com/YPS7GEURY_"


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

_CUSTOM_FIELDS_TEMPLATE = """  <n1:PODate xmlns:n1="{ns}">{po_date}</n1:PODate>
{pr_number_field}"""

_PR_NUMBER_FIELD_TEMPLATE = """  <n1:PortalPRNumber xmlns:n1="{ns}">{pr_number}</n1:PortalPRNumber>
"""

_CASH_DISCOUNT_TERMS_TEMPLATE = """  <CashDiscountTerms actionCode="01">
   <ObjectNodeSenderTechnicalID>{tech_id}</ObjectNodeSenderTechnicalID>
   <Code>{code}</Code>
  </CashDiscountTerms>
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
  <Date>{po_date}</Date>
  <n2:BusinesResidence xmlns:n2="{old_namespace}">{business_residence_id}</n2:BusinesResidence>
{buyer_party}{seller_party}{employee_responsible_party}{bill_to_party}{company_party}{ship_to_location}{purchasing_unit_party}{items}{cash_discount_terms}{custom_fields} </PurchaseOrderMaintainBundle>
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
        employee_responsible_id: str, pr_number: str = None, cash_discount_terms_code: str = None,
    ) -> dict:
        """items: [{"product_id", "quantity", "unit_of_measure",
        "unit_price", "delivery_date" (YYYY-MM-DD), "site_id"}, ...].
        `employee_responsible_id`: SAP Employee ID for EmployeeResponsibleParty
        (PartyTypeCode 167, "the Purchaser who is requesting the purchase of
        goods") - present in every single official SAP example without
        exception (Sep 4 2026 finding), so treated as effectively mandatory
        here even though not confirmed via a live PO read.
        `po_date` (YYYY-MM-DD) is sent as both the standard `Date` header
        field AND the custom `PODate` field (see module docstring, Sep 5
        2026 finding). `pr_number` (optional) is sent as the custom
        `PortalPRNumber` field if provided. `cash_discount_terms_code`
        (optional, e.g. "0010") is the Supplier Master's own Payment Terms
        code (`PurchasingData/CashDiscountTermsCode` from
        `sap_supplier_client.py`) - the create web service does NOT
        auto-derive Payment Terms from the Supplier Master like the
        interactive UI does, so it stays blank unless sent explicitly
        (Sep 5 2026 finding, official documented `CashDiscountTerms/Code`
        node).
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
        next_tech_id = 8 + len(items) * 3
        # Sep 5 2026: testing the reverse of the earlier hypothesis -
        # sending this real, documented standard party explicitly
        # (instead of relying on auto-derivation from Business
        # Residence, which never worked) to see if IT drives Business
        # Residence instead.
        purchasing_unit_party = _PARTY_TEMPLATE.format(
            tag="PurchasingUnitParty", tech_id=next_tech_id, party_id=escape(f"{purchase_unit_site}-PUR"),
        )
        next_tech_id += 1
        pr_number_field = (
            _PR_NUMBER_FIELD_TEMPLATE.format(ns=CUSTOM_FIELD_NAMESPACE, pr_number=escape(pr_number))
            if pr_number else ""
        )
        custom_fields = _CUSTOM_FIELDS_TEMPLATE.format(
            ns=CUSTOM_FIELD_NAMESPACE, po_date=escape(po_date), pr_number_field=pr_number_field,
        )
        cash_discount_terms = (
            _CASH_DISCOUNT_TERMS_TEMPLATE.format(tech_id=next_tech_id, code=escape(cash_discount_terms_code))
            if cash_discount_terms_code else ""
        )
        envelope = _ENVELOPE_TEMPLATE.format(
            namespace=NAMESPACE, currency=escape(currency), po_date=escape(po_date),
            old_namespace=OLD_CUSTOM_FIELD_NAMESPACE,
            buyer_party=buyer_party, seller_party=seller_party,
            employee_responsible_party=employee_responsible_party,
            bill_to_party=bill_to_party, company_party=company_party,
            ship_to_location=ship_to_location, purchasing_unit_party=purchasing_unit_party,
            items=items_xml, custom_fields=custom_fields,
            cash_discount_terms=cash_discount_terms,
            business_residence_id=escape(purchase_unit_site),
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
        # A real success response looks like:
        # <PurchaseOrder><ReferenceObjectNodeSenderTechnicalID>1</...>
        # <ChangeStateID>...</ChangeStateID>
        # <BusinessTransactionDocumentID>29173</BusinessTransactionDocumentID>
        # <UUID>fa163e47-...</UUID></PurchaseOrder><Log/>
        # (confirmed live Sep 4 2026, PO 29173) - NOT <PurchaseOrderID>/
        # <PurchaseOrderUUID> like the sibling read-only client's response
        # shape; this is the write confirmation's own distinct tag names.
        po_id_match = re.search(r"<BusinessTransactionDocumentID>([^<]+)</BusinessTransactionDocumentID>", resp.text)
        po_uuid_match = re.search(r"<UUID>([^<]+)</UUID>", resp.text)
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
