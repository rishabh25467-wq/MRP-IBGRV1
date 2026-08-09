"""Client for SAP Business ByDesign's native Goods and Service
Acknowledgement query (QueryGoodsAndServiceAcknowledgementInbound,
operation FindSimpleByElements) - reads the REAL physical goods receipt
date for a given Product ID, straight from SAP. This is a genuinely
separate document from the Supplier Invoice (billing) we already read via
sap_supplier_invoice_client.py - a GSA is posted when goods physically
arrive, while an invoice is the supplier's bill, often posted on a
different date. Feeds the "Receipt Date" column/panel and, longer-term,
real OTD/OTIF supplier scoring for the AI Quota engine.

Authorization: requires the `QueryGoodsAndServiceAcknowledgementInbound`
service role (operation "Find goods and service acknowledgement" only -
the sibling Manage/write service is NOT needed) on the `_EMERGENTBOM`
technical user (granted Feb 2026). WSDL confirmed endpoint:
SAP_SOAP_GSA_ENDPOINT in backend/.env.
"""
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests

from sap_rate_limiter import sap_semaphore

SOAP_ACTION = "http://sap.com/xi/A1S/Global/QueryGoodsAndServiceAcknowledgementInbound/FindSimpleByElementsRequest"


class SAPGSAError(Exception):
    pass


_DATE_FILTER = """  <SelectionByDate>
   <InclusionExclusionCode>I</InclusionExclusionCode>
   <IntervalBoundaryTypeCode>9</IntervalBoundaryTypeCode>
   <LowerBoundaryDate>{min_date}</LowerBoundaryDate>
  </SelectionByDate>
"""

_REQUEST_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">
<soapenv:Body>
<n1:GSASimpleByElementsQuery_sync xmlns:n1="http://sap.com/xi/SAPGlobal20/Global">
 <GSASimpleSelectionByElements>
{date_filter}  <SelectionByItemProductProductKeyProductID>
   <InclusionExclusionCode>I</InclusionExclusionCode>
   <IntervalBoundaryTypeCode>1</IntervalBoundaryTypeCode>
   <LowerBoundaryProductID>{product_id}</LowerBoundaryProductID>
  </SelectionByItemProductProductKeyProductID>
 </GSASimpleSelectionByElements>
 <ProcessingConditions>
  <QueryHitsMaximumNumberValue>{limit}</QueryHitsMaximumNumberValue>
  <QueryHitsUnlimitedIndicator>false</QueryHitsUnlimitedIndicator>
 </ProcessingConditions>
</n1:GSASimpleByElementsQuery_sync>
</soapenv:Body>
</soapenv:Envelope>"""


class SAPGSAClient:
    # Same cascading-recency-window strategy as sap_supplier_invoice_client.py
    # (see its docstring) - SAP's FindSimpleByElements does not return hits
    # in date order, so a capped, undated query can silently miss the
    # genuinely newest receipts on a high-volume product.
    FETCH_LIMIT = 200
    RECENT_WINDOWS_DAYS = [30, 90, 400]

    def __init__(self, endpoint: str, username: str, password: str):
        self.endpoint = endpoint
        self.username = username
        self.password = password

    def _run_query(self, product_id: str, limit: int, min_date: str = None) -> list:
        date_filter = _DATE_FILTER.format(min_date=min_date) if min_date else ""
        body = _REQUEST_TEMPLATE.format(product_id=product_id, limit=limit, date_filter=date_filter)
        try:
            with sap_semaphore:
                resp = requests.post(
                    self.endpoint,
                    auth=(self.username, self.password),
                    data=body.encode("utf-8"),
                    headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": SOAP_ACTION},
                    timeout=60,
                )
        except requests.exceptions.RequestException as e:
            raise SAPGSAError(f"Could not reach SAP: {e}")

        if resp.status_code != 200 or "Fault" in resp.text[:2000]:
            raise SAPGSAError(f"SAP Goods Receipt query failed (HTTP {resp.status_code}): {resp.text[:400]}")

        root = ET.fromstring(resp.text)
        rows = []
        for gsa in root.findall(".//GSA"):
            gsa_id = gsa.findtext("GSAID")
            posting_date = gsa.findtext("PostingDate")
            po_id = gsa.findtext("POID")
            seller = gsa.find("SupplierParty")
            supplier_internal_id = seller.findtext("PartyKey/PartyID") if seller is not None else None
            for item in gsa.findall("Item"):
                item_product_id = item.findtext("ItemIndividualMaterial/ProductID")
                if not item_product_id or item_product_id.strip().upper() != product_id.strip().upper():
                    continue
                qty_el = item.find("Quantity")
                rows.append({
                    "gsa_id": gsa_id,
                    "posting_date": posting_date,
                    "po_id": po_id,
                    "supplier_internal_id": supplier_internal_id,
                    "quantity": float(qty_el.text) if qty_el is not None and qty_el.text else None,
                    "unit_of_measure": qty_el.get("unitCode") if qty_el is not None else None,
                })
        return rows

    def get_receipt_dates_for_product(self, product_id: str, limit: int = 20) -> list:
        """Returns real SAP Goods Receipt (Goods & Service Acknowledgement)
        line items for this Product ID: [{gsa_id, posting_date, po_id,
        supplier_internal_id, quantity, unit_of_measure}, ...], sorted
        newest-first, capped to `limit` rows. See sap_supplier_invoice_client
        for why a cascading recency window (not a flat capped query) is
        required for correctness on high-volume products."""
        rows = []
        for days in self.RECENT_WINDOWS_DAYS:
            min_date = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
            rows = self._run_query(product_id, self.FETCH_LIMIT, min_date=min_date)
            if rows:
                break
        if not rows:
            rows = self._run_query(product_id, self.FETCH_LIMIT)
        rows.sort(key=lambda r: r["posting_date"] or "", reverse=True)
        return rows[:limit]
