"""Client for SAP Business ByDesign's native Supplier Invoice query
(QuerySupplierInvoiceQueryIn, operation FindSimpleByElements / SOAP action
QUERY_BY_ELEMENTS) - reads REAL, posted Supplier Invoice line items for a
given Product ID straight from SAP itself (supplier name, net unit price,
quantity, invoice date). This is the SAP-native counterpart to the external
Price Explorer (MS SQL ERP) data - useful as an authoritative cross-check,
and to prove actual purchase history exists for items that have no
Released SAP Price Specification (e.g. SCR410WM - discussed with user Feb
2026: has real invoices/moving-average cost but no formal Price Spec).

Authorization: requires the `QuerySupplierInvoiceQueryIn` service role on
the `_EMERGENTBOM` technical user (granted Feb 2026 - see
/app/SAP_SUPPLIER_INVOICE_QUERY_AUTHORIZATION_REQUEST.md). WSDL confirmed
endpoint: SAP_SOAP_SUPPLIER_INVOICE_ENDPOINT in backend/.env.
"""
import xml.etree.ElementTree as ET

import requests


class SAPSupplierInvoiceError(Exception):
    pass


_REQUEST_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">
<soapenv:Body>
<n0:SupplierInvoiceSimpleByElementsQueryMBF_sync xmlns:n0="http://sap.com/xi/SAPGlobal20/Global">
 <SupplierInvoiceSimpleSelectionByElements>
  <SelectionByItemProductProductKeyProductID>
   <InclusionExclusionCode>I</InclusionExclusionCode>
   <IntervalBoundaryTypeCode>1</IntervalBoundaryTypeCode>
   <LowerBoundaryProductID>{product_id}</LowerBoundaryProductID>
  </SelectionByItemProductProductKeyProductID>
 </SupplierInvoiceSimpleSelectionByElements>
 <ProcessingConditions>
  <QueryHitsMaximumNumberValue>{limit}</QueryHitsMaximumNumberValue>
  <QueryHitsUnlimitedIndicator>false</QueryHitsUnlimitedIndicator>
 </ProcessingConditions>
</n0:SupplierInvoiceSimpleByElementsQueryMBF_sync>
</soapenv:Body>
</soapenv:Envelope>"""


class SAPSupplierInvoiceClient:
    def __init__(self, endpoint: str, username: str, password: str):
        self.endpoint = endpoint
        self.username = username
        self.password = password

    def get_invoices_for_product(self, product_id: str, limit: int = 20) -> list:
        """Returns real SAP Supplier Invoice line items for this Product ID:
        [{invoice_id, date, supplier_name, supplier_internal_id, quantity,
        unit_of_measure, price, currency}, ...] - one row per invoice line
        (an invoice can list the same product more than once; returned
        as-is, not deduplicated, since that reflects the real SAP data)."""
        body = _REQUEST_TEMPLATE.format(product_id=product_id, limit=limit)
        try:
            resp = requests.post(
                self.endpoint,
                auth=(self.username, self.password),
                data=body.encode("utf-8"),
                headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": "QUERY_BY_ELEMENTS"},
                timeout=30,
            )
        except requests.exceptions.RequestException as e:
            raise SAPSupplierInvoiceError(f"Could not reach SAP: {e}")

        if resp.status_code != 200 or "Fault" in resp.text[:2000]:
            raise SAPSupplierInvoiceError(f"SAP Supplier Invoice query failed (HTTP {resp.status_code}): {resp.text[:400]}")

        root = ET.fromstring(resp.text)
        rows = []
        for invoice in root.findall(".//SupplierInvoice"):
            invoice_id = invoice.findtext("ID")
            date = invoice.findtext("Date")
            seller = invoice.find("SellerParty")
            supplier_name = None
            supplier_internal_id = None
            if seller is not None:
                supplier_name = seller.findtext("AddressSnapshot/FormattedAddress/FormattedName")
                supplier_internal_id = seller.findtext("PartyKey/PartyID")
            for item in invoice.findall("Item"):
                item_product_id = item.findtext("Product/ProductKey/ProductID")
                if not item_product_id or item_product_id.strip().upper() != product_id.strip().upper():
                    continue
                qty_el = item.find("Quantity")
                price_el = item.find("NetUnitPrice/Amount")
                rows.append({
                    "invoice_id": invoice_id,
                    "date": date,
                    "supplier_name": (supplier_name or "").strip() or None,
                    "supplier_internal_id": supplier_internal_id,
                    "quantity": float(qty_el.text) if qty_el is not None and qty_el.text else None,
                    "unit_of_measure": qty_el.get("unitCode") if qty_el is not None else None,
                    "price": float(price_el.text) if price_el is not None and price_el.text else None,
                    "currency": price_el.get("currencyCode") if price_el is not None else None,
                })
        rows.sort(key=lambda r: r["date"] or "", reverse=True)
        return rows
