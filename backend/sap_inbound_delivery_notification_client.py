"""SAP Business ByDesign `ManageStandardInboundDeliveryNotificationIn`
SOAP service (Sep 2026) - creates + releases a Standard Inbound Delivery
Notification directly against a supplier Purchase Order, with the
ACTUAL received quantity already set at creation time.

Why this exists: replaces the Playwright-driven "search PO -> select
row -> Post Goods Receipt -> fill Delivery Notification ID/date/qty ->
Remove Zero Quantity Items -> Save and Close" UI flow in
sap_playwright_supplier_pgr_service.py, which is fragile (SAPUI5
timing, row-matching bugs) and slow. User set up the "Input of Advanced
Shipping Notification" Communication Arrangement (Application Protocol
= Web Service) with the shared `_EMERGENTBOM` technical user (same
credentials every other SOAP client in this app already uses).

Envelope/party shape follows the SAME conventions confirmed live for
the sibling PO write client (sap_po_write_client.py): plain-text
PartyID (no child elements), `Quantity`/`DeliveryQuantity` with a
`unitCode` attribute, date periods as `StartDateTime`/`EndDateTime`
with `timeZoneCode="UTC"`, wrapper element in namespace
`http://sap.com/xi/SAPGlobal20/Global`, `<Log><Item><Note>` for
human-readable messages.

`DeliveryNotificationID` (required) is NOT SAP-assigned - it's the
external/vendor document number field (same value the old Playwright
flow typed into "Delivery Notification ID", i.e. `supplier_doc_num`) -
confirmed by this service's own name ("Advanced Shipping Notification",
designed for a 3rd party to submit its OWN reference number).

CheckMaintainBundle (validation only, commits nothing) and
MaintainBundle (real create+release) share the exact same request
payload shape - only the outer wrapper element name differs - so
`_build_envelope` is shared by both `check` and `maintain_bundle`.

UNVERIFIED end-to-end as a REAL create+release (Sep 2026) - the user
must review the first live MaintainBundle call's result before this is
trusted for unattended use in the general GRN flow (only
CheckMaintainBundle has been confirmed safe to call freely, since it
never commits anything to SAP).

Sep 18 2026 fix: added the required `<ProcessingTypeCode>SD</ProcessingTypeCode>`
field (was missing entirely) - confirmed via SAP's own official docs
(help.sap.com PSM_ISI_R_II_MANAGE_STAND_INB_NOTIF_IN, "ProcessingTypeCode
(always SD for standard notifications)"). Several other field-name
variants were floated during this investigation (VendorInternalID,
SellerParty/InternalID, BaseQty, ProcessingTypeCode=185/188, etc.) but
none matched SAP's documented schema and were NOT used here - this
module sticks to the officially documented element names only."""
import re
from xml.sax.saxutils import escape

import requests
from requests.auth import HTTPBasicAuth

from sap_rate_limiter import sap_semaphore

NAMESPACE = "http://sap.com/xi/SAPGlobal20/Global"
SOAP_ACTION = ""

_ITEM_TEMPLATE = """   <Item actionCode="01">
    <LineItemID>{item_number}</LineItemID>
    <TypeCode>14</TypeCode>
    <DeliveryQuantity unitCode="{unit_code}">{quantity}</DeliveryQuantity>
    <DeliveryQuantityTypeCode>{unit_code}</DeliveryQuantityTypeCode>
    <ItemProduct>
     <ProductID>{product_id}</ProductID>
    </ItemProduct>
    <ItemBusinessTransactionDocumentReference>
     <PurchaseOrder>
      <ID>{po_number}</ID>
      <TypeCode>001</TypeCode>
      <ItemID>{item_number}</ItemID>
     </PurchaseOrder>
    </ItemBusinessTransactionDocumentReference>
   </Item>
"""

_ENVELOPE_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">
<soapenv:Body>
<n0:{wrapper_element} xmlns:n0="{namespace}">
 <BasicMessageHeader/>
 <StandardInboundDeliveryNotification actionCode="01" releaseDocumentIndicator="{release}">
  <DeliveryNotificationID>{notification_id}</DeliveryNotificationID>
  <ProcessingTypeCode>SD</ProcessingTypeCode>
  <DeliveryDate>
   <StartDateTime timeZoneCode="UTC">{delivery_date}T00:00:00Z</StartDateTime>
   <EndDateTime timeZoneCode="UTC">{delivery_date}T23:59:59Z</EndDateTime>
  </DeliveryDate>
  <VendorID>{vendor_id}</VendorID>
{items_xml} </StandardInboundDeliveryNotification>
</n0:{wrapper_element}>
</soapenv:Body>
</soapenv:Envelope>"""


class SAPInboundDeliveryNotificationError(Exception):
    pass


class SAPInboundDeliveryNotificationNotConfiguredError(SAPInboundDeliveryNotificationError):
    pass


def _extract_fault_message(raw_xml: str) -> str:
    match = re.search(r"<faultstring[^>]*>(.*?)</faultstring>", raw_xml, re.DOTALL)
    return match.group(1).strip() if match else raw_xml[:400]


def _extract_log_notes(raw_xml: str) -> list:
    return re.findall(r"<Note[^>]*>([^<]+)</Note>", raw_xml)


def _extract_severity_codes(raw_xml: str) -> list:
    return re.findall(r"<SeverityCode[^>]*>([^<]+)</SeverityCode>", raw_xml)


class SAPInboundDeliveryNotificationClient:
    def __init__(self, endpoint: str, username: str, password: str, timeout: int = 60):
        self.endpoint = endpoint or None
        self.auth = HTTPBasicAuth(username, password)
        self.timeout = timeout

    def _build_envelope(self, wrapper_element: str, notification_id: str, po_number: str, vendor_id: str,
                        delivery_date: str, items: list, release: bool) -> str:
        items_xml = "".join(
            _ITEM_TEMPLATE.format(
                item_number=escape(str(it["item_number"])), po_number=escape(str(po_number)),
                unit_code=escape(it.get("unit_of_measure") or "EA"), quantity=it["quantity"],
                product_id=escape(str(it["product_id"])),
            ) for it in items
        )
        return _ENVELOPE_TEMPLATE.format(
            wrapper_element=wrapper_element, namespace=NAMESPACE,
            release="true" if release else "false",
            notification_id=escape(str(notification_id)), delivery_date=escape(delivery_date),
            vendor_id=escape(str(vendor_id)), items_xml=items_xml,
        )

    def _post(self, envelope: str) -> str:
        if not self.endpoint:
            raise SAPInboundDeliveryNotificationNotConfiguredError(
                "SAP Inbound Delivery Notification service isn't wired up - "
                "SAP_SOAP_INBOUND_DELIVERY_NOTIFICATION_ENDPOINT is not set."
            )
        headers = {"Content-Type": "text/xml; charset=utf-8", "SOAPAction": SOAP_ACTION}
        try:
            with sap_semaphore:
                resp = requests.post(self.endpoint, data=envelope.encode("utf-8"), headers=headers, auth=self.auth, timeout=self.timeout)
        except requests.exceptions.RequestException as e:
            raise SAPInboundDeliveryNotificationError(f"SAP Inbound Delivery Notification service unreachable: {e}")
        if resp.status_code != 200:
            raise SAPInboundDeliveryNotificationError(
                f"SAP rejected the request (HTTP {resp.status_code}): {_extract_fault_message(resp.text)}"
            )
        return resp.text

    def _parse_confirmation(self, raw_xml: str) -> dict:
        notif_id_match = re.search(r"<DeliveryNotificationID>([^<]*)</DeliveryNotificationID>", raw_xml)
        uuid_match = re.search(r"<StandardInboundDeliveryNotificationConfirmationBody>.*?<UUID>([^<]*)</UUID>", raw_xml, re.S)
        notes = _extract_log_notes(raw_xml)
        severities = _extract_severity_codes(raw_xml)
        has_error = any(s.strip() in ("3", "4") for s in severities)
        return {
            "delivery_notification_id": notif_id_match.group(1) if notif_id_match else None,
            "uuid": uuid_match.group(1) if uuid_match else None,
            "notes": notes,
            "severities": severities,
            "has_error": has_error,
            "raw_xml": raw_xml[:5000],
        }

    def check_maintain_bundle(self, notification_id: str, po_number: str, vendor_id: str,
                               delivery_date: str, items: list) -> dict:
        """Pure validation - commits NOTHING to SAP. Safe to call freely."""
        envelope = self._build_envelope(
            "StandardInboundDeliveryNotificationBundleCreateCheckRequest_sync",
            notification_id, po_number, vendor_id, delivery_date, items, release=False,
        )
        return self._parse_confirmation(self._post(envelope))

    def maintain_bundle(self, notification_id: str, po_number: str, vendor_id: str,
                         delivery_date: str, items: list, release: bool = True) -> dict:
        """REAL create (+release if release=True) - irreversible SAP write."""
        envelope = self._build_envelope(
            "StandardInboundDeliveryNotificationBundleCreateRequest_sync",
            notification_id, po_number, vendor_id, delivery_date, items, release=release,
        )
        result = self._parse_confirmation(self._post(envelope))
        if result["has_error"] or not result["delivery_notification_id"]:
            raise SAPInboundDeliveryNotificationError(
                "SAP rejected the Inbound Delivery Notification: " + ("; ".join(result["notes"]) if result["notes"] else result["raw_xml"][:400])
            )
        return result
