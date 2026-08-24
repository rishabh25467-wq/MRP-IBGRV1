"""SAP Business ByDesign "Manage Customer Requirements" SOAP client -
creates real Intra-Company Stock Transfer Orders (Aug 2026).

Confirmed live via SAP's own public docs (help.sap.com,
PSM_ISI_R_II_MANAGE_CUST_REQ_IN) plus the user-uploaded WSDL - one
endpoint, three operations differentiated by SOAPAction + request root
element:
  - ManageCustomerRequirementInCheckMaintainAsBundle (`check()` below):
    validates a payload against SAP, no commit. User's explicit ask (Aug
    2026): always run this FIRST as an always-on safety net - not a
    togglable dry-run mode - before the real write, so a malformed
    payload can never create garbage in production SAP.
  - ManageCustomerRequirementInMaintainAsBundle (`maintain()` below): the
    real, irreversible create write. Returns the real SAP-issued ID/UUID.
  - ReadCustomerRequirementInAsBundle: not used by this app.

Same raw-XML + SOAPAction-header pattern as every other SAP write client
in this codebase (see sap_production_proposal_client.py) - the schema is
fixed/documented, no zeep/runtime WSDL parsing needed.
"""
import re
from xml.sax.saxutils import escape

import requests
from requests.auth import HTTPBasicAuth

from sap_rate_limiter import sap_semaphore

_CHECK_SOAP_ACTION = "http://sap.com/xi/A1S/Global/ManageCustomerRequirementIn/ManageCustomerRequirementInCheckMaintainAsBundleRequest"
_MAINTAIN_SOAP_ACTION = "http://sap.com/xi/A1S/Global/ManageCustomerRequirementIn/ManageCustomerRequirementInMaintainAsBundleRequest"

# SAP's DeliveryPriorityCode runtime code list: 1=Immediate, 2=Urgent,
# 3=Normal, 7=Low - this app's UI fixes Delivery Priority to "Immediate".
DELIVERY_PRIORITY_IMMEDIATE = "1"
# SAP's PartialDeliveryControlCode runtime code list - "9" = Single
# delivery, full quantity only (user's explicit pick, Aug 2026, over "1"
# Multiple delivery).
PARTIAL_DELIVERY_SINGLE_FULL_QTY = "9"


class SAPSTOError(Exception):
    pass


def _first_tag(xml: str, tag: str):
    m = re.search(rf"<(?:\w+:)?{tag}(?:\s[^>]*)?>(.*?)</(?:\w+:)?{tag}>", xml, re.S)
    return re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else None


def _all_blocks(xml: str, tag: str):
    return [m.group(1) for m in re.finditer(rf"<(?:\w+:)?{tag}(?:\s[^>]*)?>(.*?)</(?:\w+:)?{tag}>", xml, re.S)]


def _log_error(xml: str):
    """This particular SAP service's <Log> block (confirmed live, Aug 2026 -
    differs from sap_goods_movement_client's SeverityCode-based Log shape)
    has NO SeverityCode at all - just <Log><Item><Note>...</Note></Item>
    ...</Log>. Per SAP's own docs ("If some errors or warnings are
    encountered... the same is reflected in the log... no messages in the
    log" on a clean success), ANY <Note> under <Log> means this
    Check/Maintain call did not go through cleanly - collects every one so
    the real cause is visible."""
    log_block = re.search(r"<(?:\w+:)?Log>(.*?)</(?:\w+:)?Log>", xml, re.S)
    if not log_block:
        return None
    notes = [n.strip() for n in re.findall(r"<Note>(.*?)</Note>", log_block.group(1), re.S) if n.strip()]
    if not notes:
        return None
    return "; ".join(notes)


def _format_qty(qty: float) -> str:
    return format(qty, "f").rstrip("0").rstrip(".") or "0"


def _build_items_xml(items: list) -> str:
    parts = []
    for idx, item in enumerate(items, start=1):
        item_id = idx * 10
        description = escape((item.get("description") or item["product_id"])[:40])
        parts.append(f"""
      <ExternalRquestItem ActionCode="01">
        <ObjectNodeSenderTechnicalID>{item_id}</ObjectNodeSenderTechnicalID>
        <ItemID>{item_id}</ItemID>
        <ProductKey>
          <ProductTypeCode></ProductTypeCode>
          <ProductIdentifierTypeCode></ProductIdentifierTypeCode>
          <ProductID>{escape(item["product_id"])}</ProductID>
        </ProductKey>
        <RequestedQuantity unitCode="{escape(item["unit_code"])}">{_format_qty(item["requested_qty"])}</RequestedQuantity>
        <RequestedLocalDateTime timeZoneCode="UTC">{item["requested_local_datetime"]}</RequestedLocalDateTime>
        <PartialDeliveryControlCode>{PARTIAL_DELIVERY_SINGLE_FULL_QTY}</PartialDeliveryControlCode>
        <Description languageCode="EN">{description}</Description>
      </ExternalRquestItem>""")
    return "".join(parts)


def _build_envelope(root_tag: str, ship_from_site_id: str, ship_to_site_id: str,
                     ship_to_location_id: str, items: list) -> str:
    items_xml = _build_items_xml(items)
    return f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">
  <soapenv:Body>
    <n0:{root_tag} xmlns:n0="http://sap.com/xi/SAPGlobal20/Global">
      <CustomerRequirement ActionCode="01">
        <ObjectNodeSenderTechnicalID>1</ObjectNodeSenderTechnicalID>
        <ChangeStateID></ChangeStateID>
        <ID></ID>
        <ShipFromSiteID>{escape(ship_from_site_id)}</ShipFromSiteID>
        <ShipToSiteID>{escape(ship_to_site_id)}</ShipToSiteID>
        <ShipToLocationID>{escape(ship_to_location_id)}</ShipToLocationID>
        <CompleteDeliveryRequestedIndicator>false</CompleteDeliveryRequestedIndicator>
        <DeliveryPriorityCode>{DELIVERY_PRIORITY_IMMEDIATE}</DeliveryPriorityCode>{items_xml}
      </CustomerRequirement>
    </n0:{root_tag}>
  </soapenv:Body>
</soapenv:Envelope>"""


class SAPSTOClient:
    def __init__(self, endpoint: str, username: str, password: str):
        self.endpoint = endpoint
        self.auth = HTTPBasicAuth(username, password)

    def _call(self, root_tag: str, soap_action: str, ship_from_site_id, ship_to_site_id, ship_to_location_id, items) -> str:
        envelope = _build_envelope(root_tag, ship_from_site_id, ship_to_site_id, ship_to_location_id, items)
        try:
            with sap_semaphore:
                resp = requests.post(
                    self.endpoint,
                    data=envelope.encode("utf-8"),
                    auth=self.auth,
                    headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": soap_action},
                    timeout=45,
                )
        except requests.exceptions.RequestException as e:
            raise SAPSTOError(f"Could not reach SAP: {e}")
        xml = resp.text
        if resp.status_code >= 400 or "<Fault" in xml or ":Fault" in xml:
            faultstring = _first_tag(xml, "faultText") or _first_tag(xml, "faultstring") or f"HTTP {resp.status_code}"
            details = [d for d in (_first_tag(b, "text") for b in _all_blocks(xml, "faultDetail")) if d]
            raise SAPSTOError(faultstring + ((" | " + "; ".join(details)) if details else ""))
        log_error = _log_error(xml)
        if log_error:
            raise SAPSTOError(log_error)
        return xml

    def check(self, ship_from_site_id: str, ship_to_site_id: str, ship_to_location_id: str, items: list) -> None:
        """Validates the payload against SAP - no commit, nothing is
        created. Raises SAPSTOError with SAP's own message if invalid."""
        self._call("CustReqBundleCheckMaintainRequest_sync", _CHECK_SOAP_ACTION,
                    ship_from_site_id, ship_to_site_id, ship_to_location_id, items)

    def maintain(self, ship_from_site_id: str, ship_to_site_id: str, ship_to_location_id: str, items: list) -> dict:
        """The real, irreversible SAP write. Returns {"id": str, "uuid": str}."""
        xml = self._call("CustReqBundleMaintainRequest_sync", _MAINTAIN_SOAP_ACTION,
                          ship_from_site_id, ship_to_site_id, ship_to_location_id, items)
        sap_id = _first_tag(xml, "ID")
        sap_uuid = _first_tag(xml, "UUID")
        if not sap_id:
            raise SAPSTOError("SAP did not return a Stock Transfer Order ID")
        return {"id": sap_id, "uuid": sap_uuid}
