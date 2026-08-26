"""SAP Business ByDesign Goods and Service Acknowledgement (GSA) WRITE
client - Supplier Portal Phase 4 (Aug 2026). Posts a real Goods Receipt
against a Purchase Order the moment internal staff approve a vendor's
shipment (physical goods + supplier invoice matched) in the GRN screen -
fully automated end-to-end, no SAP UI touch anywhere (confirmed as this
tenant's setup: all receiving already goes through this app).

Confirmed via SAP's own help docs (help.sap.com PSM_ISI_R_II_SRM_GSA_MBO)
that this is a GENUINELY DIFFERENT service from the read-only
QueryGoodsAndServiceAcknowledgementInbound already used elsewhere in this
app (sap_gsa_client.py, SAP_SOAP_GSA_ENDPOINT) - that service explicitly
cannot write. The write/maintain operation is
`ManageGoodsAndServiceAcknowledgementIn`, message body
`GSABundleMaintainRequest_sync`, linking to the PO via
<PurchaseOrderReference><BusinessTransactionDocumentReference><ID> (PO
ID) + <ItemID> (PO Item ID) + <TypeCode>001</TypeCode>.

BLOCKED as of Aug 2026, ONE thing left before go-live:
  - Live-verified Aug 26 2026: SAP_SOAP_GSA_WRITE_ENDPOINT is now configured and
    reachable (confirmed via a GET connectivity probe - HTTP 415, the
    same "endpoint exists, POST a body" signature as every other live
    SOAP client in this app). NOT yet POSTed to for real - deliberately
    held back pending the user's explicit go-ahead, since this writes a
    real Goods Receipt into their LIVE production SAP tenant (same SAP
    system regardless of preview/production app environment) - a wrong
    field name here does not fail safely, it either errors or posts bad
    data against a real Purchase Order.
  - STILL OPEN/UNVERIFIED: whether this Item node accepts an explicit
    stock-status override at all, or whether routing new stock into
    Quality Inspection is governed entirely by the Product's own
    Inspection Plan configuration in SAP (user confirmed "Quality
    Inspection stock" = TargetInventoryStockStatusCode="1" is the
    intent - but that field lives on the sibling Goods Movement schema,
    not confirmed present here). MUST be verified against a real GRN
    approval (ideally against a test/low-risk PO the user nominates)
    before this is trusted for every-day use.

post_goods_receipt raises SAPGSAWriteNotConfiguredError until
SAP_SOAP_GSA_WRITE_ENDPOINT is set - the ONLY caller is
supplier_shipment_service.approve_shipment(), which treats that as a
"queued, will sync once SAP is connected" state rather than a hard
failure of the internal approval itself."""
from xml.sax.saxutils import escape
import re

import requests
from requests.auth import HTTPBasicAuth

from sap_rate_limiter import sap_semaphore

# Namespace confirmed via SAP help docs (help.sap.com PSM_ISI_R_II_SRM_GSA_MBO):
# GSA services live under http://sap.com/xi/A1S/Global, NOT the
# SAPGlobal20 namespace used by the Purchase Order query service.
GSA_NAMESPACE = "http://sap.com/xi/A1S/Global"
# TODO: confirm the exact SOAPAction string against the real WSDL if the
# live tenant rejects an empty SOAPAction (every other client in this
# app that has been live-tested so far - PO query - accepts "").
SOAP_ACTION = ""


class SAPGSAWriteError(Exception):
    pass


class SAPGSAWriteNotConfiguredError(SAPGSAWriteError):
    pass


def _extract_fault_message(raw_xml: str) -> str:
    """SAP's SOAP faults bury the one human-readable line inside
    <faultstring>...</faultstring> behind a wall of namespace boilerplate
    - surface just that (falls back to a short truncated raw snippet if
    the shape doesn't match, e.g. a non-SOAP error page)."""
    match = re.search(r"<faultstring[^>]*>(.*?)</faultstring>", raw_xml, re.DOTALL)
    if match:
        return match.group(1).strip()
    return raw_xml[:300]


_ITEM_TEMPLATE = """  <Item>
   <BusinessTransactionDocumentTypeCode actionCode="01">18</BusinessTransactionDocumentTypeCode>
   <Quantity unitCode="{unit_code}">{quantity}</Quantity>
   <PurchaseOrderReference ActionCode="01">
    <BusinessTransactionDocumentReference>
     <ID>{po_id}</ID>
     <TypeCode>001</TypeCode>
     <ItemID>{item_id}</ItemID>
     <ItemTypeCode>18</ItemTypeCode>
    </BusinessTransactionDocumentReference>
   </PurchaseOrderReference>
  </Item>
"""

_ENVELOPE_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">
<soapenv:Body>
<n0:GSABundleMaintainRequest_sync xmlns:n0="{namespace}">
 <BasicMessageHeader/>
 <GoodsAndServiceAcknowledgement actionCode="01">
  <BusinessTransactionDocumentTypeCode>282</BusinessTransactionDocumentTypeCode>
  <Name languageCode="EN">Supplier Portal GRN {doc_code}</Name>
  <PostGSAIndicator>true</PostGSAIndicator>
{items}</GoodsAndServiceAcknowledgement>
</n0:GSABundleMaintainRequest_sync>
</soapenv:Body>
</soapenv:Envelope>"""


class SAPGSAWriteClient:
    def __init__(self, endpoint: str, username: str, password: str, timeout: int = 60):
        self.endpoint = endpoint or None
        self.auth = HTTPBasicAuth(username, password)
        self.timeout = timeout

    def post_goods_receipt(self, po_id: str, doc_code: str, items: list) -> dict:
        """items: [{"item_id": <SAP PO Item ID>, "quantity": float,
        "unit_of_measure": str}, ...]. The ONLY caller is
        supplier_shipment_service.approve_shipment()."""
        if not self.endpoint:
            raise SAPGSAWriteNotConfiguredError(
                "SAP Goods Receipt posting isn't wired up yet - waiting on the Manage/write GSA "
                "endpoint (SAP_SOAP_GSA_WRITE_ENDPOINT) and the _EMERGENTBOM technical user being "
                "granted write access to Goods and Service Acknowledgement."
            )
        items_xml = "".join(
            _ITEM_TEMPLATE.format(
                unit_code=escape(it.get("unit_of_measure") or "EA"),
                quantity=it["quantity"], po_id=escape(str(po_id)), item_id=escape(str(it["item_id"])),
            ) for it in items
        )
        envelope = _ENVELOPE_TEMPLATE.format(namespace=GSA_NAMESPACE, items=items_xml, doc_code=escape(doc_code))
        headers = {"Content-Type": "text/xml; charset=utf-8", "SOAPAction": SOAP_ACTION}
        try:
            with sap_semaphore:
                resp = requests.post(self.endpoint, data=envelope.encode("utf-8"), headers=headers, auth=self.auth, timeout=self.timeout)
        except requests.exceptions.RequestException as e:
            raise SAPGSAWriteError(f"SAP Goods Receipt service unreachable: {e}")
        if resp.status_code != 200:
            raise SAPGSAWriteError(f"SAP rejected the Goods Receipt (HTTP {resp.status_code}): {_extract_fault_message(resp.text)}")
        return {"ok": True, "raw_xml": resp.text[:2000]}
