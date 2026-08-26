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

BLOCKED as of Aug 2026, TWO separate things needed before go-live:
  1. The real Manage/write GSA endpoint URL (SAP_SOAP_GSA_WRITE_ENDPOINT,
     distinct from SAP_SOAP_GSA_ENDPOINT above - left blank in .env).
  2. The `_EMERGENTBOM` technical user granted the "Maintain goods and
     service acknowledgement" service operation (it currently only has
     "Find", per sap_gsa_client.py's docstring).
  3. STILL OPEN/UNVERIFIED even once the above two land: whether this
     Item node accepts an explicit stock-status override at all, or
     whether routing new stock into Quality Inspection is governed
     entirely by the Product's own Inspection Plan configuration in SAP
     (user confirmed "Quality Inspection stock" = TargetInventoryStockStatusCode="1"
     is the intent - but that field lives on the sibling Goods Movement
     schema, not confirmed present here). MUST be verified against the
     real WSDL/a sandbox PO before this goes live - do not trust the
     envelope below blindly, it is best-effort from public SAP docs only.

post_goods_receipt raises SAPGSAWriteNotConfiguredError until
SAP_SOAP_GSA_WRITE_ENDPOINT is set - the ONLY caller is
supplier_shipment_service.approve_shipment(), which treats that as a
"queued, will sync once SAP is connected" state rather than a hard
failure of the internal approval itself."""
from xml.sax.saxutils import escape

import requests
from requests.auth import HTTPBasicAuth

from sap_rate_limiter import sap_semaphore

# TODO: confirm the exact SOAPAction string against the real WSDL once
# the write endpoint + technical user authorization are granted.
SOAP_ACTION = "http://sap.com/xi/SAPGlobal20/Global/ManageGoodsAndServiceAcknowledgementIn/GSABundleMaintainRequestConfirmation_sync"


class SAPGSAWriteError(Exception):
    pass


class SAPGSAWriteNotConfiguredError(SAPGSAWriteError):
    pass


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
<n0:GSABundleMaintainRequest_sync xmlns:n0="http://sap.com/xi/SAPGlobal20/Global">
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
        envelope = _ENVELOPE_TEMPLATE.format(items=items_xml, doc_code=escape(doc_code))
        headers = {"Content-Type": "text/xml; charset=utf-8", "SOAPAction": SOAP_ACTION}
        try:
            with sap_semaphore:
                resp = requests.post(self.endpoint, data=envelope.encode("utf-8"), headers=headers, auth=self.auth, timeout=self.timeout)
        except requests.exceptions.RequestException as e:
            raise SAPGSAWriteError(f"SAP Goods Receipt service unreachable: {e}")
        if resp.status_code != 200:
            raise SAPGSAWriteError(f"SAP rejected the Goods Receipt (HTTP {resp.status_code}): {resp.text[:500]}")
        return {"ok": True, "raw_xml": resp.text[:2000]}
